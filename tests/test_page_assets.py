"""An article draft's figures and embeds: kept, approved and published with the article."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_clean_content_draft import notes
from test_content_draft import fixture, start
from test_procedure_publication import publication_db as publication_db

from tin_lite import approved_article, content_draft, page_assets
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.procedures import PinnedCodexProcedure, validate_codex_procedure_definition
from tin_lite.publication import OutputCheckpoint
from tin_lite.workflow_reviews import WorkflowReviews

GOOD_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><use href="#a"/><rect fill="#c33"/></svg>'
EMBED = b"<!doctype html><canvas id=c></canvas><script>draw(c)</script>"
UNSAFE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>steal()</script></svg>'


def article(folder_name):
    return (
        "# Documented messaging setup\n\n"
        + ("Follow the published API reference for the supported request fields. " * 6).rstrip()
        + f'\n\n![How a request flows](./{folder_name}/flow.svg "The request path")\n\n'
        f"```tin-embed\nsrc: ./{folder_name}/field.html\nheight: 420\ntitle: Try it\n```\n\n"
        f"![Unsafe](./{folder_name}/unsafe.svg)\n\n![Missing](./{folder_name}/missing.svg)\n"
    ).encode()


def test_the_article_declares_its_assets():
    path = "content/articles/2026-10-05-1a2b.md"
    text = (
        "![a](./2026-10-05-1a2b.assets/flow.svg)\n```tin-embed\n"
        "src: ./2026-10-05-1a2b.assets/field.html\n```\n"
        "again 2026-10-05-1a2b.assets/flow.svg, elsewhere other.assets/x.svg, "
        "./2026-10-05-1a2b.assets/old.svg.bak"
    )
    assert page_assets.referenced(text, path) == [
        "content/articles/2026-10-05-1a2b.assets/flow.svg",
        "content/articles/2026-10-05-1a2b.assets/field.html",
    ]
    assert page_assets.problem("a.svg", GOOD_SVG) is None
    assert page_assets.problem("a.html", EMBED) is None
    for bad in (
        UNSAFE_SVG,
        b'<svg onload="x()"/>',
        b'<svg><a href="https://example.com">x</a></svg>',
        b"<svg><foreignObject/></svg>",
        b"",
    ):
        assert page_assets.problem("a.svg", bad)
    assert page_assets.problem("a.html", b"\xff\xfe")


def test_only_article_drafts_declare_an_assets_folder():
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == content_draft.KEY)
    policy = page_assets.AssetPolicy(max_files=12, max_bytes=2_000_000)
    definition, _ = replace(
        spec, procedure=replace(spec.procedure, output_assets=policy)
    ).definition_and_resource_files()
    assert definition["procedure"]["output"]["assets"] == {"max_files": 12, "max_bytes": 2_000_000}
    assert validate_codex_procedure_definition(definition).output_assets == policy
    for limits in (
        {"max_files": 0, "max_bytes": 1},
        {"max_files": 1},
        {"max_files": 99, "max_bytes": 1},
    ):
        broken = {**definition, "procedure": {**definition["procedure"]}}
        broken["procedure"]["output"] = {**definition["procedure"]["output"], "assets": limits}
        with pytest.raises(ValueError, match="assets"):
            validate_codex_procedure_definition(broken)
    other = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.diagram")
    diagram, _ = other.definition_and_resource_files()
    diagram["procedure"]["output"]["assets"] = {"max_files": 1, "max_bytes": 1}
    with pytest.raises(ValueError, match="Only article drafts"):
        validate_codex_procedure_definition(diagram)
    # Definitions without assets are unchanged, so pinned runs keep their exact contract.
    plain, _ = replace(
        spec, procedure=replace(spec.procedure, output_assets=None)
    ).definition_and_resource_files()
    assert "assets" not in plain["procedure"]["output"]
    # The sandbox learns where the article's files go, and the runner's limits.
    pinned = PinnedCodexProcedure(
        workflow_key=content_draft.KEY,
        prompt="p",
        entry_skill="s",
        skill_files={},
        output_path="content/drafts/x.md",
        output_validator=content_draft.CLEAN_VALIDATOR,
        output_assets=policy,
    )
    assert pinned.sandbox_context(inputs={})["output"]["assets"] == {
        "folder": "content/drafts/x.assets",
        "max_files": 12,
        "max_bytes": 2_000_000,
    }
    assert "assets" not in replace(pinned, output_assets=None).sandbox_context(inputs={})["output"]


async def drafted(db, monkeypatch):
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda *args: None)
    f = await fixture(db, monkeypatch, clean=True, assets=True)
    run = await start(f)
    sandboxes = SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock())
    f.activities = TinActivities(
        database=f.db, storage=f.storage, settings=f.settings, sandboxes=sandboxes
    )
    await f.activities.prepare_codex_procedure(str(run.id))
    ctx = await f.service.saved(run.id)
    await f.activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    path = content_draft.PATH_TEMPLATE.format(run_id=run.id)
    folder = page_assets.folder(path)
    name = folder.rsplit("/", 1)[-1]
    base = f.storage.repo.head
    revision = f.storage.repo.edit(
        {
            path: article(name),
            content_draft.notes_path(path): notes(ctx),
            f"{folder}/flow.svg": GOOD_SVG,
            f"{folder}/field.html": EMBED,
            f"{folder}/unsafe.svg": UNSAFE_SVG,
            f"{folder}/unused.svg": GOOD_SVG,
        },
        parent=run.expected_head_sha,
    )
    f.storage.branch_revision, f.storage.repo.head = revision, base
    return f, run, sandboxes, path, folder


async def test_a_draft_keeps_approves_and_publishes_the_files_its_article_uses(
    publication_db, monkeypatch
):
    f, run, sandboxes, path, folder = await drafted(publication_db, monkeypatch)
    await f.activities.persist_codex_procedure_artifact(str(run.id))
    receipt = await f.db.get_effect(f"{run.id}:procedure_artifact_persist")
    checkpoint = OutputCheckpoint.load(receipt.result["checkpoint"], run=run)
    assert [item.artifact_path for item in checkpoint.assets] == [
        f"{folder}/flow.svg",
        f"{folder}/field.html",
    ]
    assert [item.media_type for item in checkpoint.assets] == ["image/svg+xml", "text/html"]
    # Left out with a reason, never a failed run; a file nothing refers to isn't kept at all.
    assert receipt.result["dropped_assets"] == [
        {
            "path": f"{folder}/unsafe.svg",
            "reason": "the SVG contains script, event handlers or outside references",
        },
        {"path": f"{folder}/missing.svg", "reason": "the file is missing"},
    ]

    await f.activities.commit_codex_procedure_artifact(str(run.id))
    tree = f.storage.repo.trees[f.storage.repo.head]
    assert tree[f"{folder}/flow.svg"][1] == GOOD_SVG and tree[f"{folder}/field.html"][1] == EMBED
    assert f"{folder}/unsafe.svg" not in tree and f"{folder}/unused.svg" not in tree

    # Approval binds the assets with the article.
    assert await f.activities.request_codex_procedure_review(str(run.id))
    run = await f.db.get_run(run.id)
    reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    _, artifact = await reviews.artifact(run)
    bound = [
        {"path": f"{folder}/flow.svg", "sha256": hashlib.sha256(GOOD_SVG).hexdigest()},
        {"path": f"{folder}/field.html", "sha256": hashlib.sha256(EMBED).hexdigest()},
    ]
    assert artifact["assets"] == bound
    published = await f.db.get_effect(f"{run.id}:procedure_canonical_commit")
    assert approved_article._review_artifact(run, published.result)["assets"] == bound

    # Delivery re-reads each approved file and refuses one that changed.
    repo_id = (await f.db.get_project(run.project_id)).state_repo_id
    assets = await page_assets.verified(
        f.storage, repo_id=repo_id, revision=run.canonical_commit_sha, binding=bound
    )
    assert [(a["media_type"], a["bytes"]) for a in assets] == [
        ("image/svg+xml", len(GOOD_SVG)),
        ("text/html", len(EMBED)),
    ]
    assert page_assets.source_binding({"assets": assets}) == {"assets": bound}
    changed = f.storage.repo.edit({f"{folder}/flow.svg": UNSAFE_SVG})
    with pytest.raises(ValueError, match="differs from what was approved"):
        await page_assets.verified(f.storage, repo_id=repo_id, revision=changed, binding=bound)


def test_a_saved_checkpoint_keeps_assets_inside_the_articles_folder():
    run = SimpleNamespace(
        id="00000000-0000-4000-8000-0000000000aa",
        project_id="00000000-0000-4000-8000-0000000000bb",
        generation=1,
        definition_commit_sha="a" * 40,
        expected_head_sha="b" * 40,
    )
    path = "content/drafts/x.md"

    def item(asset_path, media_type="image/svg+xml", revision="c" * 40):
        return OutputCheckpoint.create(
            run=run, revision=revision, path=asset_path, media_type=media_type, content=GOOD_SVG
        )

    good = OutputCheckpoint.create(
        run=run,
        revision="c" * 40,
        path=path,
        media_type="text/markdown",
        content=b"# A\n",
        assets=(item("content/drafts/x.assets/flow.svg"),),
    )
    assert good.files[-1].artifact_path == "content/drafts/x.assets/flow.svg"
    assert OutputCheckpoint.load(good.to_dict(), run=run) == good
    # A checkpoint without assets serializes exactly as before.
    assert (
        "assets"
        not in OutputCheckpoint.create(
            run=run, revision="c" * 40, path=path, media_type="text/markdown", content=b"# A\n"
        ).to_dict()
    )
    for bad in (
        item("content/drafts/other.assets/flow.svg"),
        item("content/drafts/x.assets/flow.svg", media_type="text/html"),
        item("content/drafts/x.assets/flow.svg", revision="d" * 40),
    ):
        with pytest.raises(ValueError, match="assets"):
            OutputCheckpoint.create(
                run=run,
                revision="c" * 40,
                path=path,
                media_type="text/markdown",
                content=b"# A\n",
                assets=(bad,),
            )


@pytest.mark.parametrize("ready", [True, False])
async def test_the_sandbox_gets_the_folder_and_an_old_image_refuses_it_early(monkeypatch, ready):
    import base64
    import json

    from tin_lite.e2b_runtime import E2BRuntime, SandboxProcedureInput

    payload = base64.b64encode(json.dumps({"summary": "Done.", "message": "Ok."}).encode())
    seen = {}

    class Handle:
        async def wait(self):
            return SimpleNamespace(
                stdout=f"TIN_PROCEDURE_COMMIT_SHA={'c' * 40}\n"
                f"TIN_PROCEDURE_RESULT={payload.decode()}\n"
            )

    class Commands:
        async def run(self, *args, **kwargs):
            command = args[0]
            if command.endswith("isolated-procedure check"):
                return SimpleNamespace(stdout="TIN_ISOLATION_READY_V1")
            if command.endswith("codex_api_config.py --check"):
                return SimpleNamespace(stdout="TIN_CODEX_API_READY_V1")
            if command.endswith("--check-companion"):
                return SimpleNamespace(stdout="TIN_PROCEDURE_COMPANION_V1")
            if command.endswith("--check-assets"):
                return SimpleNamespace(stdout="TIN_PROCEDURE_ASSETS_V1" if ready else "")
            seen.update(kwargs["envs"])
            return Handle()

    class Sandbox:
        commands = Commands()

        class files:  # noqa: N801
            @staticmethod
            async def write(path, data):
                return None

        @staticmethod
        async def kill():
            return None

    async def connect(*args, **kwargs):
        return Sandbox()

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    runtime = E2BRuntime(
        api_key="e2b-test",  # noqa: S106
        template="tin-lite-codex",
        timeout_seconds=60,
        egress_allow_hosts=(),
    )
    output = {
        "kind": "project.artifact",
        "path": "content/drafts/x.md",
        "companion_path": "content/drafts/x.generation.md",
        "companion_max_bytes": 24_000,
        "assets": {"folder": "content/drafts/x.assets", "max_files": 12, "max_bytes": 2_000_000},
    }
    run = runtime.run_procedure_and_kill(
        sandbox_id="sandbox-1",
        run_input=SandboxProcedureInput(
            execution_key="run:procedure_artifact_persist",
            isolated=True,
            usage_sink=AsyncMock(),
            api_url="https://tin.test/relay",
            api_grant="synthetic-relay-grant",
            canonical_url="https://storage.test/canonical.git",
            canonical_auth_header="Authorization: Basic canonical",
            canonical_branch="main",
            ephemeral_url="https://storage.test/ephemeral.git",
            ephemeral_auth_header="Authorization: Basic ephemeral",
            ephemeral_branch="procedures/run/1",
            proxy_url="http://proxy.test:3128",
            no_proxy="tin.test",
            context={"workflow_key": content_draft.KEY, "output": output},
            output_path="content/drafts/x.md",
            output_max_bytes=80_000,
            result_kind="project.artifact",
        ),
    )
    if not ready:
        with pytest.raises(RuntimeError, match="lacks article assets support"):
            await run
        assert not seen
        return
    await run
    assert seen["TIN_PROCEDURE_ASSETS_FOLDER"] == "content/drafts/x.assets"
    assert seen["TIN_PROCEDURE_ASSETS_MAX_FILES"] == "12"
    assert seen["TIN_PROCEDURE_ASSETS_MAX_BYTES"] == "2000000"


async def test_a_revision_receives_the_approved_assets(publication_db, monkeypatch):
    from tin_lite.workflow_reviews import revision_context

    f, run, _, _, folder = await drafted(publication_db, monkeypatch)
    await f.activities.persist_codex_procedure_artifact(str(run.id))
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    run = await f.db.get_run(run.id)
    _, artifact = await WorkflowReviews(runtime=f.runtime, settings=f.settings).artifact(run)
    command = {
        "artifact": artifact,
        "reference_files": [],
        "feedback": "Make the figure clearer.",
        "root_run_id": run.id,
        "source_run_id": run.id,
    }
    context = await revision_context(f.db, f.storage, command, run)
    assert [a["path"] for a in context["source"]["assets"]] == [
        f"{folder}/flow.svg",
        f"{folder}/field.html",
    ]
    from tin_lite.article_review import revision_prompt

    assert "this run's own assets folder" in revision_prompt(context)


def test_the_wording_check_skips_embed_video_and_diagram_blocks():
    from tin_lite.content_repository_delivery import FIGURE_FENCE

    text = (
        "Intro words here.\n\n```tin-embed\nsrc: ./a.assets/x.html\nheight: 420\n```\n\n"
        "After the embed.\n\n~~~mermaid\ngraph LR\nA-->B\n~~~\n\n```python\nprint(1)\n```\n"
    )
    kept = FIGURE_FENCE.sub("\n\n", text)
    assert "height" not in kept and "graph LR" not in kept
    assert "Intro words here." in kept and "print(1)" in kept


def test_the_reader_renders_figures_embeds_videos_and_callouts():
    from tin_lite.documents import render_markdown

    markdown = (
        '# T\n\n![Flow](./a.assets/flow.svg "What to notice")\n\n'
        "```tin-embed\nsrc: ./a.assets/field.html\nheight: 9999\ntitle: Try <b>it</b>\n```\n\n"
        "```tin-embed\nsrc: ../../secrets.html\n```\n\n"
        "```tin-video\nurl: https://www.youtube.com/watch?v=dQw4w9WgXcQ\ntitle: Trailer\n```\n\n"
        "```tin-video\nurl: javascript:alert(1)\n```\n\n"
        "> [!WARNING]\n> Mind the gate.\n\n> A plain quote.\n"
    )
    html = render_markdown(markdown, asset_folder="content/articles/a.assets").html
    # A figure has no URL; the reader loads it with the member's session.
    assert 'class="md-asset" data-asset="content/articles/a.assets/flow.svg"' in html
    assert 'src="./a.assets' not in html
    assert 'data-height="1600" data-title="Try &lt;b&gt;it&lt;/b&gt;"' in html
    # A path outside the folder and a script URL stay plain code.
    assert "src: ../../secrets.html" in html and "javascript:alert" in html
    assert html.count('class="md-embed"') == 1 and html.count('class="md-video"') == 1
    assert '<aside class="md-callout" data-kind="warning">' in html
    assert "<blockquote>\n<p>A plain quote.</p>" in html
    # Any other document renders as before.
    plain = render_markdown(markdown).html
    assert 'src="./a.assets/flow.svg"' in plain and "md-embed" not in plain


async def test_the_review_shows_the_bundle_and_says_what_it_carries(publication_db, monkeypatch):
    import httpx
    from test_private_workflows import app

    f, run, _, path, folder = await drafted(publication_db, monkeypatch)
    await f.activities.persist_codex_procedure_artifact(str(run.id))
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    assert await f.activities.request_codex_procedure_review(str(run.id))
    explanation = await f.db.pool.fetchval(
        "SELECT explanation FROM run_decisions WHERE run_id=$1", run.id
    )
    assert "With 1 figure and 1 interactive piece." in explanation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        document = (await client.get(f"/api/workflows/runs/{run.id}/artifact/document")).json()
        run = await f.db.get_run(run.id)
        in_files = (
            await client.get(
                f"/api/projects/{run.project_id}/files/document",
                params={"path": path, "revision": run.canonical_commit_sha},
            )
        ).json()
    kept = [
        {"path": f"{folder}/flow.svg", "media_type": "image/svg+xml"},
        {"path": f"{folder}/field.html", "media_type": "text/html"},
    ]
    assert document["assets"] == kept
    assert document["asset_notes"] == [
        "unsafe.svg was left out: the SVG contains script, event handlers or outside references.",
        "missing.svg was left out: the file is missing.",
    ]
    assert f'data-asset="{folder}/flow.svg"' in document["html"]
    assert f'class="md-embed" data-asset="{folder}/field.html"' in document["html"]
    # Files shows the same article with the files it refers to that exist there.
    assert in_files["assets"] == kept


def bundle_source(article, assets=()):
    from tin_lite.content_repository_delivery import validate_copy  # noqa: F401

    return {
        "article": article,
        "article_sha256": hashlib.sha256(article.encode()).hexdigest(),
        "binding": {"repository": "owner/site", "head_sha": "9" * 40},
        "assets": [
            {"path": path, "sha256": hashlib.sha256(raw).hexdigest()} for path, raw in assets
        ],
    }


def test_delivery_confirms_every_figure_embed_diagram_and_video_arrived():
    from tin_lite.content_repository_delivery import figure_check, merge_rule, validate_copy

    article = (
        "# Filming the trailers\n\nEvery shot follows recorded game events in order.\n\n"
        "![Flow](./a.assets/flow.svg)\n\n```tin-embed\nsrc: ./a.assets/field.html\n```\n\n"
        "```mermaid\ngraph LR\nA-->B\n```\n\n"
        "```tin-video\nurl: https://www.youtube.com/watch?v=dQw4w9WgXcQ\ntitle: Trailer\n```\n"
    )
    source = bundle_source(
        article, [("content/a.assets/flow.svg", GOOD_SVG), ("content/a.assets/field.html", EMBED)]
    )
    page = (
        "<h1>Filming the trailers</h1><p>Every shot follows recorded game events in order.</p>"
        '<img src="flow.svg"><iframe src="field.html"></iframe>'
        '<iframe src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"></iframe>'
    )
    manifest = {
        "repository": "owner/site",
        "head_sha": "9" * 40,
        "body": "Public URL: https://example.com/blog/filming",
        "files": [
            {"path": "public/blog/filming/index.html", "content": page},
            {"path": "public/blog/filming/flow.svg", "content": GOOD_SVG.decode()},
            {"path": "public/blog/filming/field.html", "content": EMBED.decode()},
            {"path": "public/blog/filming/diagram-1.svg", "content": "<svg></svg>"},
        ],
    }
    proof = validate_copy(manifest, source)
    assert proof["copy_check"] == "wording_preserved" and proof["figure_check"] == "confirmed"
    assert merge_rule(manifest, proof, "/blog/{slug}") == "chosen_route"
    # A missing embed, a changed figure, a missing video or diagram: the merge waits.
    for drop in ("field.html", "flow.svg", "youtube", "diagram-1.svg"):
        broken = {
            **manifest,
            "files": [
                {**item, "content": item["content"].replace("youtube-nocookie", "vimeo")}
                if drop == "youtube"
                else item
                for item in manifest["files"]
                if drop == "youtube" or not item["path"].endswith(drop)
            ],
        }
        if drop == "youtube":
            broken["files"][0]["content"] = broken["files"][0]["content"].replace(
                "dQw4w9WgXcQ", "other"
            )
        proof = validate_copy(broken, source)
        assert proof["figure_check"] == "not_confirmed", drop
        assert merge_rule(broken, proof, "/blog/{slug}") is None
    # A page without figures has no figure check at all.
    assert figure_check(manifest, bundle_source("# Plain\n\nWords.\n")) is None


def test_a_delivery_with_assets_reads_them_where_they_were_approved():
    from tin_lite.activities import procedure_project_revision

    run = SimpleNamespace(expected_head_sha="a" * 40)
    procedure = SimpleNamespace(output_validator=None, review_revision_context=None)
    with_assets = {"source_revision": "b" * 40, "assets": [{"path": "x", "sha256": "y"}]}
    assert procedure_project_revision(run, procedure, with_assets) == "b" * 40
    assert procedure_project_revision(run, procedure, {"source_revision": "b" * 40}) is None
    assert procedure_project_revision(run, procedure, None) is None


def test_github_writes_fit_a_page_with_its_figures():
    from tin_lite.integrations import PULL_REQUEST_MAX_BYTES, PULL_REQUEST_MAX_FILES
    from tin_lite.procedures import DEFAULT_PULL_REQUEST_BYTES, GitHubPullRequestProcedure

    assert (PULL_REQUEST_MAX_FILES, PULL_REQUEST_MAX_BYTES) == (30, 2_000_000)
    # Procedures that declare nothing keep their existing 512 KB contract.
    assert GitHubPullRequestProcedure("r/{run_id}.md", ()).max_bytes == DEFAULT_PULL_REQUEST_BYTES
    deliver = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.deliver")
    output = deliver.definition_and_resource_files()[0]["procedure"]["output"]
    assert (output["max_files"], output["max_bytes"]) == (30, 2_000_000)
