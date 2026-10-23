"""website.change, source analytics: Tin Analytics' line, CSP entries and reporter on the site.

Set up proposes one `analytics` row when GitHub's website repository takes pull requests; the
founder approves its pieces (the line, the CSP entries, the reporter by SHA-256) in Decisions,
which starts website.change. Codex writes the patch; `check_patch` holds it to the pieces and Tin
writes the reporter's published bytes itself. Stop measuring proposes taking them out again.

The patch check runs on plain inputs; the flow runs on the website.change harness with the fake
Agent Traffic service (tests/agent_traffic_fake.py) serving the reporter files.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from agent_traffic_fake import REPORTER_FILES, FakeAgentTraffic, reporter_sha256
from test_agent_traffic_connection import analytics, card
from test_procedure_publication import publication_db as publication_db
from test_website_change_audit import ACTOR, merge_outcome, mergeable
from test_website_change_audit import fixture as audit_fixture

from tin_lite import content_repository_delivery as delivery
from tin_lite import website_change
from tin_lite import website_change_analytics as analytics_change
from tin_lite.agent_traffic_connection import PROVIDER, site_from
from tin_lite.integrations import ServiceCallRefused, site_folder
from tin_lite.organic_audit import canonical_json
from tin_lite.procedures import procedure_checkpoint_path

SITE = "st-fake0001"
SCRIPT = f'<script async src="https://edge.tin.computer/sdk/v1.js" data-site="{SITE}"></script>'
OLD_SCRIPT = SCRIPT.replace(SITE, "st-old00001")
CSP = {"script-src": ["https://edge.tin.computer"], "connect-src": ["https://edge.tin.computer"]}
NEXT_REPORTER = {
    "variant": "nextjs",
    "version": "1",
    "url": "https://edge.tin.computer/reporter/v1/nextjs.js",
    "sha256": reporter_sha256("nextjs"),
    "content": REPORTER_FILES["nextjs"],
}
LAYOUT = """export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
"""
LAYOUT_WITH_LINE = LAYOUT.replace(
    '<html lang="en">\n', f'<html lang="en">\n      <head>{SCRIPT}</head>\n'
)
MIDDLEWARE = """import { withTinReporter } from "./tin-reporter.js";
export default withTinReporter();
export const config = {
  matcher: ["/((?!api/|_next/static|_next/image|.*\\\\.(?:js|css)$).*)"],
};
"""
NEXT_CONFIG = """module.exports = {
  async headers() {
    return [{ source: "/(.*)", headers: [{ key: "Content-Security-Policy",
      value: "default-src 'self'; script-src 'self'; connect-src 'self'" }] }];
  },
};
"""
REPO = {
    "app/layout.tsx": LAYOUT,
    "app/page.tsx": "export default function Home() { return <h1>Sheepdogs</h1>; }\n",
    "next.config.js": NEXT_CONFIG,
    "package.json": '{"dependencies": {"next": "15.1.0", "react": "19.0.0"}}\n',
}


def source(kind="install", *, reporter=True, previous=None, folder=""):
    detail = {
        "repository": "owner/site",
        "host": "example.com",
        "site_id": SITE,
        "script": SCRIPT,
        "csp": CSP,
        "reporter": (
            {**{k: NEXT_REPORTER[k] for k in ("variant", "version", "url", "sha256")}}
            if reporter
            else None
        ),
        "folder": folder,
    }
    if previous:
        detail["previous"] = previous
    return {"change": {"kind": kind, "detail": detail}}


def manifest(*files, outcome="patch"):
    return {"files": [{"path": path, "content": text} for path, text in files], "outcome": outcome}


# --- The patch check ------------------------------------------------------------------------


def test_an_install_adds_exactly_the_pieces_and_the_reporter_bytes_are_tins():
    retyped = REPORTER_FILES["nextjs"].replace("  return handler;", "    return handler;")
    files, problems = analytics_change.check_patch(
        manifest(
            ("app/layout.tsx", LAYOUT_WITH_LINE),
            ("tin-reporter.js", retyped),
            ("middleware.ts", MIDDLEWARE),
            ("next.config.js", NEXT_CONFIG.replace("'self';", "'self' https://edge.tin.computer;")),
        ),
        {
            "app/layout.tsx": LAYOUT,
            "tin-reporter.js": None,
            "middleware.ts": None,
            "next.config.js": NEXT_CONFIG,
        },
        source(),
        NEXT_REPORTER,
    )
    assert problems == []
    # The session retyped the reporter; the pull request carries the published file.
    copy = next(item for item in files if item["path"] == "tin-reporter.js")
    assert copy["content"] == REPORTER_FILES["nextjs"]
    assert analytics_change.reporter_path(files, NEXT_REPORTER) == "tin-reporter.js"


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        (
            ("app/layout.tsx", LAYOUT_WITH_LINE.replace("<body>", "<body className='dark'>")),
            "className",
        ),
        (
            (
                "app/layout.tsx",
                LAYOUT_WITH_LINE.replace(
                    "</head>", "<script>fetch('https://collector.example/x')</script></head>"
                ),
            ),
            "fetch",
        ),
        (("app/layout.tsx", LAYOUT.replace('lang="en"', 'lang="tr"')), "removes other text"),
        (
            ("app/page.tsx", "export default function Home() { return null; }\n"),
            "removes other text",
        ),
        (("app/analytics.tsx", "export const id = 1;\n"), "It adds app/analytics.tsx"),
    ],
)
def test_any_other_edit_holds_the_pull_request_for_the_founder(changed, reason):
    originals = {"app/layout.tsx": LAYOUT, "app/page.tsx": REPO["app/page.tsx"]}
    files = (
        [changed]
        if changed[0] == "app/layout.tsx"
        else [("app/layout.tsx", LAYOUT_WITH_LINE), changed]
    )
    _files, problems = analytics_change.check_patch(
        manifest(*files), {**originals, "app/analytics.tsx": None}, source(reporter=False), None
    )
    assert problems and reason in " ".join(problems)


def test_dependencies_ci_and_secrets_are_refused_outright():
    for path in ("package.json", ".github/workflows/deploy.yml", ".env.production"):
        with pytest.raises(analytics_change.PatchRefused, match=path):
            analytics_change.check_patch(
                manifest(("app/layout.tsx", LAYOUT_WITH_LINE), (path, "x\n")),
                {"app/layout.tsx": LAYOUT, path: "y\n"},
                source(reporter=False),
                None,
            )


def test_a_patch_without_the_line_or_over_an_existing_file_waits():
    _files, problems = analytics_change.check_patch(
        manifest(("middleware.ts", MIDDLEWARE), ("tin-reporter.js", REPORTER_FILES["nextjs"])),
        {"middleware.ts": None, "tin-reporter.js": "// the site's own file\n"},
        source(),
        NEXT_REPORTER,
    )
    assert "It doesn't add the line." in problems
    assert "It writes the reporter over the existing tin-reporter.js." in problems


def test_a_replace_swaps_only_the_line_and_a_remove_only_takes_out():
    old = LAYOUT.replace(
        '<html lang="en">\n', f'<html lang="en">\n      <head>{OLD_SCRIPT}</head>\n'
    )
    previous = {"script": OLD_SCRIPT, "site_id": "st-old00001"}
    files, problems = analytics_change.check_patch(
        manifest(("app/layout.tsx", old.replace(OLD_SCRIPT, SCRIPT))),
        {"app/layout.tsx": old},
        source("replace", reporter=False, previous=previous),
        None,
    )
    assert problems == [] and files[0]["content"].count(SCRIPT) == 1
    _files, problems = analytics_change.check_patch(
        manifest(("app/layout.tsx", old.replace(OLD_SCRIPT, SCRIPT).replace("en", "tr"))),
        {"app/layout.tsx": old},
        source("replace", reporter=False, previous=previous),
        None,
    )
    assert problems == ["It changes more than the line in app/layout.tsx."]
    # Remove: the line and the wiring come out of the site's own files, nothing else.
    wired = (
        'import { withTinReporter } from "./tin-reporter.js";\n'
        "export default withTinReporter(clerk());\n"
    )
    remove = source("remove", reporter=False, previous={"script": SCRIPT, "site_id": SITE})
    _files, problems = analytics_change.check_patch(
        manifest(("app/layout.tsx", LAYOUT), ("middleware.ts", "export default clerk();\n")),
        {"app/layout.tsx": LAYOUT_WITH_LINE, "middleware.ts": wired},
        remove,
        NEXT_REPORTER,
    )
    assert problems == []
    _files, problems = analytics_change.check_patch(
        manifest(("app/layout.tsx", LAYOUT.replace("<body>{children}</body>", ""))),
        {"app/layout.tsx": LAYOUT_WITH_LINE},
        remove,
        None,
    )
    assert problems and "removes 'body" in problems[0]


def test_a_site_in_a_folder_takes_its_wiring_there():
    # ClawMessenger's site is web/, with Clerk's middleware already in web/src/middleware.ts.
    clerk = (
        'import { clerkMiddleware } from "@clerk/nextjs/server";\n'
        "export default clerkMiddleware();\n"
    )
    wrapped = (
        'import { clerkMiddleware } from "@clerk/nextjs/server";\n'
        'import { withTinReporter } from "../tin-reporter.js";\n'
        "export default withTinReporter(clerkMiddleware());\n"
    )
    layout = "web/src/app/layout.tsx"
    files, problems = analytics_change.check_patch(
        manifest(
            (layout, LAYOUT_WITH_LINE),
            ("web/src/middleware.ts", wrapped),
            ("web/tin-reporter.js", REPORTER_FILES["nextjs"]),
        ),
        {layout: LAYOUT, "web/src/middleware.ts": clerk, "web/tin-reporter.js": None},
        source(folder="web"),
        NEXT_REPORTER,
    )
    assert problems == []
    assert analytics_change.reporter_path(files, NEXT_REPORTER, "web") == "web/tin-reporter.js"
    # A new middleware file is wiring only inside the site's folder.
    _files, problems = analytics_change.check_patch(
        manifest((layout, LAYOUT_WITH_LINE), ("middleware.ts", MIDDLEWARE)),
        {layout: LAYOUT, "middleware.ts": None},
        source(folder="web"),
        NEXT_REPORTER,
    )
    assert "It adds middleware.ts." in problems
    # Vercel runs Routing Middleware from the site folder's root, so that's where its copy goes.
    vercel = {**NEXT_REPORTER, "variant": "vercel"}
    assert analytics_change.is_reporter("site/middleware.js", "vercel", "site")
    assert not analytics_change.is_reporter("middleware.js", "vercel", "site")
    assert (
        analytics_change.reporter_path(
            [{"path": "site/middleware.js", "content": ""}], vercel, "site"
        )
        == "site/middleware.js"
    )


def test_the_shared_layout_takes_the_line_even_when_it_wraps_sign_in():
    # ClawMessenger: web/src/app/layout.tsx wraps /sign-in and /sign-up too. That layout is
    # where the line goes; only a protected page's own files are off limits.
    roots = ["/sign-in", "/sign-up", "/auth-complete"]
    assert website_change.file_protected("web/src/app/layout.tsx", roots) is None
    assert (
        website_change.file_protected("web/src/app/sign-in/[[...sign-in]]/page.tsx", roots)
        == "/sign-in"
    )
    skill = (
        Path(__file__).parents[1] / "codex_procedures/website.change/skills/site-analytics/SKILL.md"
    ).read_text()
    assert "goes in this shared layout even when the" in skill
    assert "Protected pages under the shared layout are never a reason for this." in skill


def test_the_reporter_follows_the_repository_and_the_host():
    detect = analytics_change.detect_variant
    host = analytics_change.detect_host
    assert (
        detect({"names": ["package.json"], "package.json": REPO["package.json"]}, None) == "nextjs"
    )
    assert detect({"names": ["next.config.ts"]}, "vercel") == "nextjs"
    # The host's own reporter sees each response's status, so it comes before Next.js's.
    assert detect({"names": ["next.config.mjs"]}, "netlify") == "netlify"
    assert (
        detect({"names": ["netlify.toml", "index.html"]}, host({"netlify.toml"}, None)) == "netlify"
    )
    worker = {"names": ["wrangler.toml"], "wrangler": 'name = "site"\nmain = "src/index.ts"\n'}
    assert detect(worker, host({"wrangler.toml"}, None)) == "cloudflare-worker"
    # keketerminal.com: Next.js built for a Worker with OpenNext; the reporter wraps the Worker.
    opennext = {**worker, "names": ["next.config.ts", "wrangler.jsonc"]}
    opennext["wrangler"] = '{\n  "name": "keke-site",\n  "main": "worker.ts",\n}'
    assert detect(opennext, "cloudflare") == "cloudflare-worker"
    # OpenNext's own entry is generated on every build, so Next.js's middleware reports instead.
    generated = {**opennext, "wrangler": '{"main": ".open-next/worker.js"}'}
    assert detect(generated, "cloudflare") == "nextjs"
    assert detect({**generated, "names": ["wrangler.jsonc"]}, "cloudflare") is None
    pages = {"names": ["wrangler.toml"], "wrangler": 'pages_build_output_dir = "dist"\n'}
    assert detect(pages, "cloudflare") is None
    assert detect({"names": ["vercel.json", "astro.config.mjs"]}, "vercel") == "vercel"
    express = {"names": ["package.json"], "package.json": '{"dependencies": {"express": "4"}}'}
    assert detect(express, None) == "node"
    assert detect({"names": ["index.html"]}, None) is None
    # The setup answer names the host when the repository doesn't.
    assert host(set(), "Next.js on Vercel") == "vercel"
    assert host({"vercel.json"}, "Netlify") == "vercel"
    # A vercel.json kept after a move to Cloudflare: the setup answer picks, or Tin doesn't guess.
    both = {"vercel.json", "wrangler.jsonc", "next.config.ts"}
    assert host(both, "Cloudflare Workers through OpenNext") == "cloudflare"
    assert host(both, None) is None
    assert host(both, "Netlify") is None


def test_the_site_folder_is_the_root_or_the_one_folder_nearest_it():
    folder = site_folder
    # ClawMessenger: Next.js in web/, beside packages that aren't sites.
    claw = ["mcp/package.json", "plugin/package.json", "web/next.config.ts", "web/vercel.json"]
    assert folder(["README.md", *claw]) == "web"
    # keketerminal.com: the site in client/, Workers of its own deeper under server/.
    keke = [
        "client/next.config.ts",
        "client/wrangler.jsonc",
        "server/cloudflare/curate-runner/wrangler.jsonc",
        "server/cloudflare/museum-img/wrangler.toml",
    ]
    assert folder(keke) == "client"
    assert folder(["apps/web/next.config.mjs", "package.json"]) == "apps/web"
    assert folder(["next.config.js", "docs/vercel.json"]) == ""
    assert folder(["index.html", "package.json"]) == ""
    # Two sites side by side: Tin doesn't guess, so the change carries the line alone.
    assert folder(["site/next.config.js", "docs/next.config.js"]) == ""
    assert folder(["node_modules/x/next.config.js", ".vercel/vercel.json", "web/vercel.json"]) == (
        "web"
    )


# --- Set up, the card and the reporter's value ----------------------------------------------


async def connect_github(service, project_id):
    await service._database.upsert_integration_connection(
        project_id=project_id,
        provider_key="infra.github",
        external_account_id="456",
        external_account_label="owner/site",
        configuration={
            "selected_repository": "owner/site",
            "write_opted_in": True,
            "permissions": {"contents": "write", "pull_requests": "write"},
        },
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=ACTOR,
    )
    service.github_site_files = AsyncMock(
        return_value={"folder": "", "names": list(REPO), "package.json": REPO["package.json"]}
    )


async def set_hosting(db, project_id, hosting):
    await db.pool.execute(
        "UPDATE workflow_runs SET input = input || jsonb_build_object('system_hosting', $2::text) "
        "WHERE project_id = $1 AND executor = 'growth.onboarding'",
        project_id,
        hosting,
    )


async def test_set_up_with_github_puts_the_change_in_decisions(publication_db):
    service, api, project = await analytics(publication_db)
    await connect_github(service, project.id)
    await set_hosting(publication_db, project.id, "Next.js on Vercel")
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=ACTOR)
    [row] = await website_change.list_changes(publication_db, project_id=project.id)
    assert (row["source"], row["kind"], row["status"], row["title"]) == (
        "analytics",
        "install",
        "pending",
        "Add Tin Analytics to example.com",
    )
    detail = row["detail"]
    assert detail["repository"] == "owner/site" and detail["reporter_host"] == "vercel"
    assert detail["reporter"]["variant"] == "nextjs"
    assert detail["reporter"]["sha256"] == reporter_sha256("nextjs")
    assert detail["script"].startswith("<script async") and detail["csp"] == CSP
    view = await card(service, project.id)
    assert view["install"]["state"] == "decide" and view["reporter_step"] is None
    # Setting up again with the same site keeps the one row.
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=ACTOR)
    assert len(await website_change.list_changes(publication_db, project_id=project.id)) == 1


async def test_without_github_the_card_names_the_builder_to_paste_into(publication_db):
    service, api, project = await analytics(publication_db, "https://sheepdogs.io")
    await set_hosting(publication_db, project.id, "Webflow, hosted by Webflow")
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=ACTOR)
    assert await website_change.list_changes(publication_db, project_id=project.id) == []
    view = await card(service, project.id)
    assert view["install"]["state"] is None
    assert view["builder"]["key"] == "webflow" and view["builder"]["source"] == "setup"
    assert view["builder"]["where"].startswith("In Webflow: Site settings → Custom code")
    await service.agent_traffic.set_builder(project_id=project.id, builder="framer")
    view = await card(service, project.id)
    assert (view["builder"]["key"], view["builder"]["source"]) == ("framer", "chosen")


async def test_a_linked_site_gets_no_change_and_needs_no_reporter(publication_db):
    service, api, project = await analytics(publication_db)
    api.linked[f"tin:{project.id}"] = ("organic-tin", ["tin.computer", "*.tin.computer"])
    await connect_github(service, project.id)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=ACTOR)
    assert await website_change.list_changes(publication_db, project_id=project.id) == []
    view = await card(service, project.id)
    assert "install" not in view and "reporter_step" not in view
    with pytest.raises(ServiceCallRefused, match="needs no reporter"):
        await service.agent_traffic.reporter_value(project_id=project.id, clerk_user_id=ACTOR)


async def test_the_reporter_value_is_sealed_shown_again_and_renewed(publication_db):
    service, api, project = await analytics(publication_db)
    await service.agent_traffic.set_up(project_id=project.id, clerk_user_id=ACTOR)
    first = await service.agent_traffic.reporter_value(project_id=project.id, clerk_user_id=ACTOR)
    assert first["env_name"] == "TIN_REPORTER_TOKEN" and first["value"].startswith(
        "reporter-token-"
    )
    stored = await publication_db.pool.fetchrow(
        "SELECT * FROM agent_traffic_sites WHERE project_id = $1", project.id
    )
    sealed = bytes(stored["reporter_token_ciphertext"])
    assert sealed and first["value"].encode() not in sealed
    again = await service.agent_traffic.reporter_value(project_id=project.id, clerk_user_id=ACTOR)
    assert again["value"] == first["value"]
    assert len(api.paths("POST")) == 1
    renewed = await service.agent_traffic.reporter_value(
        project_id=project.id, clerk_user_id=ACTOR, new=True
    )
    assert renewed["value"] != first["value"] and renewed["previous_expires_at"]
    # The card and the coding agent's guide never carry the value.
    guide = await service.agent_traffic.install_guide(project.id)
    assert renewed["value"] not in json.dumps(await card(service, project.id), default=str)
    assert renewed["value"] not in json.dumps(guide, default=str)
    assert guide["reporter"]["variants"][0]["file"] == "tin-reporter.js"
    # Asking for the guide opens the reporter step on the card, for the founder to paste.
    step = (await card(service, project.id))["reporter_step"]
    assert step["via"] == "agent" and step["env_name"] == "TIN_REPORTER_TOKEN"


# --- Approve, make, merge, stop -------------------------------------------------------------


async def site_fixture(db, monkeypatch, repo=None):
    f = await audit_fixture(db, monkeypatch, repo=repo or REPO)
    f.api = FakeAgentTraffic()
    ref = f"tin:{f.project.id}"
    body = json.loads(f.api._put(ref, {"url": "https://www.example.com"}).read())
    configuration, _token, _state = site_from(body, ref)
    await db.upsert_integration_connection(
        project_id=f.project.id,
        provider_key=PROVIDER,
        external_account_id=configuration["site_id"],
        external_account_label="www.example.com",
        configuration=configuration,
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=ACTOR,
    )
    f.configuration = configuration
    integrations = f.runtime.integrations
    integrations._database = db
    integrations._client = httpx.AsyncClient(transport=httpx.MockTransport(f.api))
    integrations.github_site_files = AsyncMock(
        return_value={"folder": "", "names": list(REPO), "package.json": REPO["package.json"]}
    )
    return f


async def approve_and_start(f, row):
    decided = await website_change.decide(
        f.db,
        project_id=f.project.id,
        change_id=row["change_id"],
        action="approve",
        actor=ACTOR,
        request_id=uuid4(),
        content_sha256=row["content_sha256"],
    )
    run, reason = await analytics_change.start_after_approval(
        runtime=f.runtime, settings=f.settings, project_id=f.project.id, change=decided, actor=ACTOR
    )
    assert reason is None, reason
    return run


async def made(f, run, files):
    """The run as the procedure makes it, up to the opened pull request."""
    assert await f.activities.prepare_codex_procedure(str(run.id)) is False
    await f.activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    checkpoint = {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": f.binding.head_sha,
        "title": "Add Tin Analytics to example.com",
        "body": "Adds the line to app/layout.tsx and the Next.js reporter. Build not run.",
        "files": [{"path": path, "content": text} for path, text in files],
        "outcome": "patch",
        "reason": "",
        "verification": [delivery.CHECK_COMMAND],
    }
    path = procedure_checkpoint_path(run.id)
    head = f.storage.repo.head
    revision = f.storage.repo.edit({path: canonical_json(checkpoint)}, parent=run.expected_head_sha)
    f.storage.branch_revision, f.storage.repo.head = revision, head
    key = f"{run.id}:procedure_artifact_persist"
    async with f.db.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await f.db.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={"ephemeral_commit_sha": revision, "checkpoint_path": path},
            sandbox_killed=True,
        )
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    await f.db.pool.execute("UPDATE workflow_runs SET status='succeeded' WHERE id=$1", run.id)
    return await f.db.get_run(run.id)


INSTALL_FILES = (
    ("app/layout.tsx", LAYOUT_WITH_LINE),
    ("tin-reporter.js", "// retyped by the session\n"),
    ("middleware.ts", MIDDLEWARE),
)


async def test_an_approved_install_opens_one_pull_request_and_merges(publication_db, monkeypatch):
    f = await site_fixture(publication_db, monkeypatch)
    row = await analytics_change.propose_install(
        f.runtime.integrations,
        project_id=f.project.id,
        configuration=f.configuration,
        host="example.com",
    )
    run = await approve_and_start(f, row)
    assert run.input["source"] == "analytics" and run.input["change_id"] == row["change_id"]
    source = await delivery.saved_source(f.db, run.id)
    assert source["publish"]["mode"] == "direct" and source["change"]["approval"]["by"] == ACTOR
    run = await made(f, run, INSTALL_FILES)
    # The pull request carries the published reporter file, whatever the session wrote.
    sent = f.runtime.integrations.github_create_pull_request.await_args.kwargs
    files = {item.path: item.content for item in sent["files"]}
    assert files["tin-reporter.js"] == REPORTER_FILES["nextjs"]
    assert files["app/layout.tsx"] == LAYOUT_WITH_LINE
    mergeable(f)
    result = await merge_outcome(f, run)
    assert result["status"] == "merged" and result["merge_rule"] == "approved_changes"
    assert result["reporter"]["path"] == "tin-reporter.js"
    assert set(result["new_files"]) == {"tin-reporter.js", "middleware.ts"}
    view = await analytics_change.install_view(f.db, f.project.id, f.configuration["site_id"])
    assert view["state"] == "added" and view["added"]["number"] == 42
    # A second start for the same change is refused: Tin already made it.
    change = await website_change.get_change(
        f.db, project_id=f.project.id, change_id=row["change_id"]
    )
    again, reason = await analytics_change.start_after_approval(
        runtime=f.runtime, settings=f.settings, project_id=f.project.id, change=change, actor=ACTOR
    )
    assert again is None and "already made" in reason
    # Stop measuring: the change that takes the line out, with what Tin added to delete.
    removal = await analytics_change.propose_remove(
        f.runtime.integrations,
        project_id=f.project.id,
        configuration=f.configuration,
        host="example.com",
    )
    assert removal["kind"] == "remove" and removal["detail"]["previous"]["script"] == SCRIPT
    assert set(removal["detail"]["previous"]["new_files"]) == {"tin-reporter.js", "middleware.ts"}
    stopped = await analytics_change.install_view(f.db, f.project.id, None)
    assert (stopped["state"], stopped["change"]["kind"]) == ("decide", "remove")
    # Set up again before approving it: a new site, whose line replaces the old one on the site.
    renewed = {
        **f.configuration,
        "site_id": "st-fake0002",
        "script": SCRIPT.replace(SITE, "st-fake0002"),
    }
    await f.db.upsert_integration_connection(
        project_id=f.project.id,
        provider_key=PROVIDER,
        external_account_id="st-fake0002",
        external_account_label="www.example.com",
        configuration=renewed,
        credential_ciphertext=None,
        credential_key_version=None,
        connected_by_clerk_user_id=ACTOR,
    )
    replace = await analytics_change.propose_install(
        f.runtime.integrations, project_id=f.project.id, configuration=renewed, host="example.com"
    )
    assert replace["kind"] == "replace" and replace["detail"]["previous"]["script"] == SCRIPT
    assert replace["detail"]["reporter"] is None
    pending = await website_change.list_changes(f.db, project_id=f.project.id, status="pending")
    assert [row["kind"] for row in pending] == ["replace"]
    view = await analytics_change.install_view(f.db, f.project.id, "st-fake0002")
    assert view["state"] == "decide" and view["added"]["reporter"]["path"] == "tin-reporter.js"


async def test_an_install_with_another_edit_waits_for_the_founder(publication_db, monkeypatch):
    f = await site_fixture(publication_db, monkeypatch)
    row = await analytics_change.propose_install(
        f.runtime.integrations,
        project_id=f.project.id,
        configuration=f.configuration,
        host="example.com",
    )
    run = await approve_and_start(f, row)
    run = await made(
        f,
        run,
        (*INSTALL_FILES, ("app/page.tsx", "export default function Home() { return null; }\n")),
    )
    mergeable(f)
    result = await merge_outcome(f, run)
    assert result["status"] == "left_open"
    assert "removes other text in app/page.tsx" in result["reason"]
    f.runtime.integrations.github_merge_pull_request.assert_not_awaited()
    view = await analytics_change.install_view(f.db, f.project.id, f.configuration["site_id"])
    assert view["state"] == "left_open" and view["pull_request"]["number"] == 42


async def test_a_reporter_that_doesnt_match_its_sha256_stops_before_the_session(
    publication_db, monkeypatch
):
    f = await site_fixture(publication_db, monkeypatch)
    row = await analytics_change.propose_install(
        f.runtime.integrations,
        project_id=f.project.id,
        configuration=f.configuration,
        host="example.com",
    )
    run = await approve_and_start(f, row)
    monkeypatch.setitem(REPORTER_FILES, "nextjs", REPORTER_FILES["nextjs"] + "// changed\n")
    with pytest.raises(ValueError, match="doesn't match the SHA-256 you approved"):
        await f.activities.prepare_codex_procedure(str(run.id))


async def test_stopping_before_the_merge_leaves_the_pull_request_for_the_founder(
    publication_db, monkeypatch
):
    f = await site_fixture(publication_db, monkeypatch)
    row = await analytics_change.propose_install(
        f.runtime.integrations,
        project_id=f.project.id,
        configuration=f.configuration,
        host="example.com",
    )
    run = await approve_and_start(f, row)
    run = await made(f, run, INSTALL_FILES)
    await f.db.pool.execute(
        "DELETE FROM integration_connections WHERE project_id=$1 AND provider_key=$2",
        f.project.id,
        PROVIDER,
    )
    mergeable(f)
    result = await merge_outcome(f, run)
    assert result["status"] == "left_open" and "stopped measuring" in result["reason"]


def test_the_workspace_hands_codex_the_pieces_and_the_exact_reporter():
    pinned = {
        **source(),
        "publish": {"mode": "direct", "reason": "approved"},
        "protected_paths": ["/sign-in"],
    }
    pinned["change"].update(change_id="an_" + "1" * 20, title="Add Tin Analytics to example.com")
    context = analytics_change.workspace(pinned, NEXT_REPORTER)
    pieces = context["analytics"]
    assert context["source"] == "analytics" and pieces["script"] == SCRIPT
    assert pieces["reporter"]["content"] == REPORTER_FILES["nextjs"]
    assert pieces["reporter"]["wiring"].startswith("// Tin request reporter for Next.js")
    assert pieces["reporter"]["file_names"] == ["tin-reporter.js"]
    assert context["protected_paths"] == ["/sign-in"]
