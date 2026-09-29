"""outreach.awesome_submit and infra.github_user: packets, placement, the founder's GitHub
grant, fork-and-pull-request and issue submission with recovery, and the approved apply."""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError
from test_integrations import PROJECT_ID, USER_ID, FakeIntegrationDatabase, settings
from test_procedure_publication import publication_db as publication_db

from tin_lite import awesome_submit as aw
from tin_lite import awesome_submit_activities as activities_module
from tin_lite import github_account as gh
from tin_lite.awesome_submit_activities import AwesomeSubmitActivities
from tin_lite.domain import EffectReceipt, RunStatus
from tin_lite.integrations import (
    GITHUB_USER_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationRequirement,
    IntegrationService,
    registered_integrations,
)

TOKEN = "gho_" + "t" * 36
LIST = "someone/awesome-cli"
README = """# Awesome CLI

## Command line

- [Alpha](https://alpha.dev) - Does alpha things.
- [Zeta](https://zeta.dev) - Does zeta things.
  - [Zeta plugin](https://zeta.dev/plugin) - An indented sub-item.

## Libraries

- [Lib](https://lib.dev) - A library.
"""
PACKETS = {
    "version": 1,
    "product": {
        "name": "Acme",
        "url": "https://acme.dev",
        "repository_url": "https://github.com/acme/acme",
    },
    "submissions": [
        {
            "list": LIST,
            "method": "pull_request",
            "path": "README.md",
            "section": "## Command line",
            "order": "alphabetical",
            "entry": "- [Acme](https://github.com/acme/acme) - Turn logs into alerts.",
            "title": "Add Acme",
            "commit_message": "Add Acme",
            "body": "Adds Acme to Command line.\n\n- [x] Alphabetical\n- [x] Not already listed",
        },
        {
            "list": "other/awesome-things",
            "method": "issue",
            "entry": "- [Acme](https://acme.dev) - Turn logs into alerts.",
            "title": "Suggestion: Acme",
            "body": "Please consider Acme for the Tools section: https://acme.dev",
        },
    ],
}


def report(packets=PACKETS) -> str:
    return (
        "# Awesome lists: Acme\n\nSome prose.\n\n## Submissions\n\n"
        f"```json awesome-submissions\n{json.dumps(packets, indent=2)}\n```\n"
    )


# ---------------------------------------------------------------- packets and placement


def test_packets_parse_validate_and_select() -> None:
    packets = aw.parse_packets(report())
    assert packets["product"]["links"] == ["https://acme.dev", "https://github.com/acme/acme"]
    assert [s["list"] for s in packets["submissions"]] == [LIST, "other/awesome-things"]
    assert aw.select(packets, [])[0]["list"] == LIST
    assert [s["list"] for s in aw.select(packets, ["other/awesome-things"])] == [
        "other/awesome-things"
    ]
    with pytest.raises(aw.PacketError, match="no submission for"):
        aw.select(packets, ["nobody/awesome"])

    def broken(**change):
        item = {**PACKETS["submissions"][0], **change}
        rejected = aw.parse_packets(report({**PACKETS, "submissions": [item]}))["rejected"]
        if rejected:
            raise aw.PacketError(rejected[0]["reason"])

    with pytest.raises(aw.PacketError, match="does not link the product"):
        broken(entry="- [Acme](https://elsewhere.dev) - Something.")
    with pytest.raises(aw.PacketError, match="owner/name"):
        broken(list="not a repo")
    with pytest.raises(aw.PacketError, match="Markdown heading"):
        broken(section="Command line")
    with pytest.raises(aw.PacketError, match="one line"):
        broken(entry="- [Acme](https://acme.dev)\n- [Evil](https://x.dev)")
    with pytest.raises(aw.PacketError, match="Markdown path"):
        broken(path="../../etc/passwd")
    with pytest.raises(aw.PacketError, match="no submissions block"):
        aw.parse_packets("# A report without the block")
    duplicate = {**PACKETS, "submissions": [PACKETS["submissions"][0]] * 2}
    assert aw.parse_packets(report(duplicate))["rejected"] == [
        {"list": LIST, "reason": "the report names this list twice"}
    ]
    with pytest.raises(ValueError, match="at most"):
        aw.check_inputs({"lists": [f"o/l{i}" for i in range(6)]})


def test_place_alphabetical_end_and_refusals() -> None:
    links = ["https://acme.dev", "https://github.com/acme/acme"]
    entry = PACKETS["submissions"][0]["entry"]
    placed = aw.place(
        README, section="## Command line", entry=entry, order="alphabetical", links=links
    )
    lines = placed["content"].splitlines()
    # Sorted by link text: Acme goes before Alpha.
    assert lines.index(entry) + 1 == lines.index(
        "- [Alpha](https://alpha.dev) - Does alpha things."
    )
    assert placed["context"]["after"][0].startswith("- [Alpha]")
    later = aw.place(
        README,
        section="## Command line",
        entry="- [Beta](https://acme.dev) - Sorted between.",
        order="alphabetical",
        links=links,
    )["content"].splitlines()
    assert later[later.index("- [Beta](https://acme.dev) - Sorted between.") - 1].startswith(
        "- [Alpha]"
    )
    # "end" goes after the last item's indented sub-items, never between them.
    placed = aw.place(README, section="## Command line", entry=entry, order="end", links=links)
    lines = placed["content"].splitlines()
    assert lines[lines.index(entry) - 1].strip().startswith("- [Zeta plugin]")
    assert placed["content"].count(entry) == 1
    assert "## Libraries" in placed["content"]
    # Line endings are kept.
    crlf = aw.place(
        README.replace("\n", "\r\n"),
        section="## Command line",
        entry=entry,
        order="alphabetical",
        links=links,
    )
    assert crlf["content"].count("\r\n") == README.count("\n") + 1
    with pytest.raises(aw.PacketError, match="already in this list"):
        aw.place(
            README + "- [Acme](https://www.acme.dev/) - Listed.\n",
            section="## Libraries",
            entry=entry,
            order="end",
            links=links,
        )
    with pytest.raises(aw.PacketError, match="no longer has that section"):
        aw.place(README, section="## Missing", entry=entry, order="end", links=links)
    with pytest.raises(aw.PacketError, match="no list items"):
        aw.place(
            "# L\n\n## Empty\n\nNothing here.\n",
            section="## Empty",
            entry=entry,
            order="end",
            links=links,
        )


def test_plan_shows_the_exact_change_account_and_disclosure() -> None:
    body = aw.with_disclosure("Adds Acme.", "Acme")
    assert body.endswith(aw.DISCLOSURE.format(product="Acme"))
    assert aw.with_disclosure(body, "Acme") == body
    change = {
        "list": LIST,
        "method": "pull_request",
        "path": "README.md",
        "section": "## Command line",
        "base_commit": "a" * 40,
        "line": 6,
        "context": {"before": ["- [Alpha](https://alpha.dev)"], "after": ["- [Zeta]"]},
        "entry": "- [Acme](https://acme.dev) - Alerts.",
        "title": "Add Acme",
        "commit_message": "Add Acme",
        "body": body,
    }
    plan = aw.render_plan(
        account="founder",
        product="Acme",
        changes=[change],
        skipped=[{"list": "x/y", "reason": "The list's repository is archived."}],
    )
    assert "@founder" in plan and "+- [Acme](https://acme.dev) - Alerts." in plan
    assert "Nothing is sent before you approve" in plan and "archived" in plan


# ---------------------------------------------------------------- a GitHub stand-in


class GitHub:
    def __init__(self, *, login="founder", scope="public_repo"):
        self.login, self.scope = login, scope
        self.requests: list[httpx.Request] = []
        self.repos = {
            LIST: {"default_branch": "main", "archived": False, "has_issues": True},
            "other/awesome-things": {
                "default_branch": "main",
                "archived": False,
                "has_issues": True,
            },
        }
        self.refs = {(LIST, "main"): "c" * 40}
        self.files = {(LIST, "README.md", "c" * 40): README}
        self.forks: set[str] = set()
        self.pulls: list[dict] = []
        self.issues: list[dict] = []
        self.fail_pull_once = False
        self.revoked = False
        self.token_valid = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            if form.get("code") != ["good-code"] or not form.get("code_verifier"):
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(
                200, json={"access_token": TOKEN, "scope": self.scope, "token_type": "bearer"}
            )
        authorized = self.token_valid and request.headers.get("authorization") == f"Bearer {TOKEN}"
        if not authorized and not path.startswith("/applications/"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx.Response(200, json={"id": 4242, "login": self.login})
        if method == "DELETE" and path.startswith("/applications/"):
            self.revoked = True
            return httpx.Response(204)
        parts = path.split("/")
        repo = "/".join(parts[2:4])
        rest = "/".join(parts[4:])
        if method == "GET" and rest == "":
            if repo in self.repos:
                return httpx.Response(
                    200, json={"full_name": repo, "private": False, **self.repos[repo]}
                )
            if repo in self.forks:
                return httpx.Response(200, json={"full_name": repo, "default_branch": "main"})
            return httpx.Response(404)
        if method == "GET" and rest.startswith("git/ref/heads/"):
            sha = self.refs.get((repo, rest.removeprefix("git/ref/heads/")))
            return (
                httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404)
            )
        if method == "GET" and rest.startswith("contents/"):
            file_path, ref = rest.removeprefix("contents/"), request.url.params["ref"]
            sha = self.refs.get((repo, ref), ref)
            content = self.files.get((repo, file_path, sha))
            if content is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": len(content),
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode()).decode(),
                    "sha": hashlib.sha256(content.encode()).hexdigest(),
                },
            )
        if method == "POST" and rest == "forks":
            fork = f"{self.login}/{repo.split('/')[1]}"
            self.forks.add(fork)
            self.refs[(fork, "main")] = self.refs[(repo, "main")]
            self.files[(fork, "README.md", self.refs[(repo, "main")])] = README
            return httpx.Response(202, json={"full_name": fork})
        if method == "POST" and rest == "git/refs":
            body = json.loads(request.content)
            branch = body["ref"].removeprefix("refs/heads/")
            if (repo, branch) in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[(repo, branch)] = body["sha"]
            self.files[(repo, "README.md", branch)] = self.files[(repo, "README.md", body["sha"])]
            return httpx.Response(201, json={})
        if method == "PUT" and rest.startswith("contents/"):
            body = json.loads(request.content)
            file_path = rest.removeprefix("contents/")
            self.files[(repo, file_path, body["branch"])] = base64.b64decode(
                body["content"]
            ).decode()
            return httpx.Response(200, json={})
        if rest == "pulls":
            if method == "GET":
                head = request.url.params["head"]
                found = [p for p in self.pulls if p["repo"] == repo and p["head"] == head]
                return httpx.Response(200, json=found)
            body = json.loads(request.content)
            pull = {
                "repo": repo,
                "head": body["head"],
                "number": len(self.pulls) + 1,
                "html_url": f"https://github.com/{repo}/pull/{len(self.pulls) + 1}",
                "body": body["body"],
                "title": body["title"],
            }
            self.pulls.append(pull)
            if self.fail_pull_once:
                self.fail_pull_once = False
                return httpx.Response(502)
            return httpx.Response(201, json=pull)
        if rest == "issues":
            if method == "GET":
                found = [i for i in self.issues if i["repo"] == repo]
                return httpx.Response(200, json=found)
            body = json.loads(request.content)
            issue = {
                "repo": repo,
                "number": len(self.issues) + 1,
                "title": body["title"],
                "html_url": f"https://github.com/{repo}/issues/{len(self.issues) + 1}",
            }
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        return httpx.Response(404)

    def branch_file(self, fork: str, branch: str) -> str:
        return self.files[(fork, "README.md", branch)]


def github_settings(**overrides):
    return settings(
        github_oauth_client_id="Iv1.client",
        github_oauth_client_secret=SecretStr("client-secret"),
        **overrides,
    )


async def service_for(api: GitHub):
    database = FakeIntegrationDatabase()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return (
        IntegrationService(database=database, settings=github_settings(), client=client),
        database,
        client,
    )


async def connect(service, api):
    started = await service.start_connect(
        project_id=PROJECT_ID, provider_key=GITHUB_USER_PROVIDER, clerk_user_id=USER_ID
    )
    query = parse_qs(urlsplit(started.authorization_url).query)
    connection = await service.github_account.complete(
        state=query["state"][0], code="good-code", clerk_user_id=USER_ID
    )
    return connection, query


@pytest.fixture(autouse=True)
def fast_fork(monkeypatch):
    monkeypatch.setattr(gh, "FORK_READY_SECONDS", 0)


# ---------------------------------------------------------------- the grant


def test_registry_declares_a_narrow_founder_account_provider() -> None:
    definition = next(d for d in registered_integrations() if d.key == GITHUB_USER_PROVIDER)
    assert set(definition.capabilities) == {
        "forks.write",
        "public_pull_requests.write",
        "public_issues.write",
    }
    assert not IntegrationService(
        database=FakeIntegrationDatabase(), settings=settings()
    ).is_configured(GITHUB_USER_PROVIDER)


async def test_connect_uses_pkce_public_repo_and_stores_the_token_encrypted() -> None:
    api = GitHub()
    service, database, client = await service_for(api)
    async with client:
        connection, query = await connect(service, api)
        again = await service.github_account.complete(
            state=query["state"][0], code="good-code", clerk_user_id=USER_ID
        )
        assert again.id == connection.id
    assert query["scope"] == ["public_repo"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://lite.tin.test/integrations/callback/github-account"]
    assert sum(r.url.path == "/login/oauth/access_token" for r in api.requests) == 1
    assert connection.external_account_label == "@founder"
    assert connection.configuration["login"] == "founder"
    visible = json.dumps([connection.configuration, database.activities], default=str)
    assert TOKEN not in visible and TOKEN.encode() not in connection.credential_ciphertext
    await service.ensure_requirements(
        project_id=PROJECT_ID,
        requirements=(
            IntegrationRequirement(GITHUB_USER_PROVIDER, ("public_pull_requests.write",), True),
        ),
    )


async def test_connect_refuses_a_grant_without_public_repo_and_disconnect_revokes() -> None:
    api = GitHub(scope="read:user")
    service, database, client = await service_for(api)
    async with client:
        with pytest.raises(IntegrationAuthorizationError, match="public repository"):
            await connect(service, api)
        assert database.connections == {}
        api.scope = "public_repo"
        await connect(service, api)
        assert await service.disconnect(project_id=PROJECT_ID, provider_key=GITHUB_USER_PROVIDER)
    assert api.revoked and database.connections == {}


# ---------------------------------------------------------------- submissions


async def test_pull_request_forks_commits_the_placed_line_and_recovers() -> None:
    api = GitHub()
    service, database, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        accounts = service.github_account
        current = await accounts.list_file(connection, LIST, "README.md")
        entry = PACKETS["submissions"][0]["entry"]
        placed = aw.place(
            current["content"],
            section="## Command line",
            entry=entry,
            order="alphabetical",
            links=["https://github.com/acme/acme"],
        )
        request = dict(
            execution_key="awesome_submit:project:list:someone/awesome-cli",
            upstream=LIST,
            default_branch="main",
            base_commit=current["base_commit"],
            path="README.md",
            content=placed["content"],
            title="Add Acme",
            body="Adds Acme.",
            commit_message="Add Acme",
        )
        api.fail_pull_once = True  # GitHub opened it, but the answer was lost.
        with pytest.raises(Exception, match="could not complete"):
            await accounts.submit_pull_request(connection, **request)
        sent = await accounts.submit_pull_request(connection, **request)
    assert len(api.pulls) == 1 and sent["recovered"] is True
    assert sent["url"] == f"https://github.com/{LIST}/pull/1"
    pull = api.pulls[0]
    branch = gh.branch_name(request["execution_key"])
    assert pull["head"] == f"founder:{branch}"
    committed = api.branch_file("founder/awesome-cli", branch)
    assert committed.count(entry) == 1 and committed.replace(entry + "\n", "") == README
    # Nothing was written to the founder's own repositories or to the list itself.
    writes = [r for r in api.requests if r.method in {"PUT", "POST", "PATCH", "DELETE"}]
    assert {r.url.path.split("/")[2] for r in writes if r.url.path.startswith("/repos/")} <= {
        "founder",
        "someone",
    }
    assert all(
        r.url.path.startswith("/repos/founder/") or r.url.path.endswith(("/forks", "/pulls"))
        for r in writes
        if r.url.path.startswith("/repos/")
    )


async def test_issue_is_looked_up_before_it_is_opened() -> None:
    api = GitHub()
    service, _, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        first = await service.github_account.open_issue(
            connection, upstream="other/awesome-things", title="Suggestion: Acme", body="Body"
        )
        second = await service.github_account.open_issue(
            connection, upstream="other/awesome-things", title="Suggestion: Acme", body="Body"
        )
    assert first["number"] == second["number"] == 1 and len(api.issues) == 1
    assert second["recovered"] is True


async def test_a_revoked_grant_marks_the_connection_for_reconnection() -> None:
    api = GitHub()
    service, database, client = await service_for(api)
    async with client:
        connection, _ = await connect(service, api)
        api.token_valid = False
        with pytest.raises(IntegrationAuthorizationError, match="Reconnect"):
            await service.github_account.repository(connection, LIST)
    stored = database.connections[(PROJECT_ID, GITHUB_USER_PROVIDER)]
    assert stored.status == "needs_attention"


# ---------------------------------------------------------------- the approved workflow


class RunDatabase:
    """Just enough of Database for the submission activities, over the integration fake."""

    def __init__(self, integrations_db, *, run, source_run, source_workflow, project):
        self.integrations_db = integrations_db
        self.runs = {run.id: run, source_run.id: source_run}
        self.workflow = source_workflow
        self.project = project
        self.effects: dict[str, EffectReceipt] = {
            f"{source_run.id}:procedure_canonical_commit": EffectReceipt(
                execution_key="x",
                operation="procedure",
                status="completed",
                result={
                    "canonical_commit_sha": source_run.canonical_commit_sha,
                    "artifact_path": aw.source_path(source_run.id),
                },
            )
        }
        self.review_requested = None
        self.projected = None
        self.failed = None
        self.pool = SimpleNamespace(fetchval=self._latest)

    async def _latest(self, *_args):
        return next(r.id for r in self.runs.values() if r.executor != aw.KEY)

    async def get_run(self, run_id, conn=None):
        return self.runs.get(run_id)

    async def get_workflow(self, workflow_id):
        return self.workflow

    async def get_project(self, project_id):
        return self.project

    async def get_effect(self, key):
        return self.effects.get(key)

    @asynccontextmanager
    async def effect_lock(self, key, operation):
        yield object(), self.effects.get(key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.effects.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", None)
        )

    async def save_effect_progress(self, conn, *, execution_key, result):
        current = self.effects[execution_key]
        assert current.status == "started"
        self.effects[execution_key] = EffectReceipt(
            execution_key, current.operation, "started", result
        )

    async def complete_effect(self, conn, *, execution_key, result):
        current = self.effects[execution_key]
        self.effects[execution_key] = EffectReceipt(
            execution_key, current.operation, "completed", result
        )

    async def save_publication_intent(self, conn, **_):
        return None

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id):
        yield

    async def mark_run_running(self, run_id):
        return None

    async def project_run_progress(self, **_):
        return None

    async def request_human_review(self, **values):
        self.review_requested = values
        self.runs[values["run_id"]].status = RunStatus.NEEDS_INPUT
        return True

    async def record_human_review(self, *, run_id, decision, summary):
        self.runs[run_id].review_decision = decision

    async def complete_awesome_submit_projection(self, conn, **values):
        self.projected = values

    async def project_failure(self, *, run_id, error_message):
        self.failed = error_message

    def __getattr__(self, name):
        return getattr(self.integrations_db, name)


async def submission_setup(api, monkeypatch, *, packets=PACKETS):
    service, integrations_db, client = await service_for(api)
    project = SimpleNamespace(id=PROJECT_ID, state_repo_id="state", canonical_branch="main")
    source_id = uuid4()
    source = SimpleNamespace(
        id=source_id,
        project_id=PROJECT_ID,
        workflow_id=uuid4(),
        executor="codex.procedure",
        status=RunStatus.SUCCEEDED,
        artifact_path=aw.source_path(source_id),
        canonical_commit_sha="d" * 40,
    )
    run = SimpleNamespace(
        id=uuid4(),
        project_id=PROJECT_ID,
        executor=aw.KEY,
        status=RunStatus.RUNNING,
        input={"project_id": str(PROJECT_ID)},
        review_decision=None,
    )
    database = RunDatabase(
        integrations_db,
        run=run,
        source_run=source,
        source_workflow=SimpleNamespace(key="outreach.awesome_lists"),
        project=project,
    )
    published: list[dict] = []

    async def publish_artifacts(**values):
        published.append(values["documents"])
        return "e" * 40

    async def read_canonical_artifact(**_):
        return report(packets).encode()

    monkeypatch.setattr(activities_module, "publish_artifacts", publish_artifacts)
    storage = SimpleNamespace(read_canonical_artifact=read_canonical_artifact)
    service._database = database
    activities = AwesomeSubmitActivities(database=database, storage=storage, integrations=service)
    return activities, database, run, client, service, published


async def test_nothing_is_sent_before_approval_and_each_list_only_once(monkeypatch) -> None:
    api = GitHub()
    activities, database, run, client, service, published = await submission_setup(api, monkeypatch)
    async with client:
        await connect(service, api)
        run_id = str(run.id)
        await activities.prepare(run_id)
        await activities.draft(run_id)
        await activities.request_review(run_id)
        assert api.pulls == [] and api.issues == [] and not api.forks
        plan = next(iter(published[0].values())).decode()
        assert "@founder" in plan and LIST in plan and "other/awesome-things" in plan
        assert "Approve and send 2" in database.review_requested["summary"]
        with pytest.raises(ApplicationError, match="not approved"):
            await activities.apply(run_id)
        await activities.record_approval(run_id)
        await activities.apply(run_id)
        await activities.apply(run_id)  # a retried activity replays its receipt
        await activities.publish(run_id)
        assert len(api.pulls) == 1 and len(api.issues) == 1
        assert aw.DISCLOSURE.format(product="Acme") in api.pulls[0]["body"]
        assert database.projected["summary"].startswith("Sent 2 of 2")

        # A later run for the same project never submits to these lists again.
        second = SimpleNamespace(**{**run.__dict__, "id": uuid4(), "review_decision": None})
        database.runs[second.id] = second
        await activities.prepare(str(second.id))
        with pytest.raises(ApplicationError, match="No list could take"):
            await activities.draft(str(second.id))
    assert len(api.pulls) == 1 and len(api.issues) == 1


async def test_a_different_connected_account_is_refused_at_apply(monkeypatch) -> None:
    api = GitHub()
    activities, database, run, client, service, _ = await submission_setup(api, monkeypatch)
    async with client:
        await connect(service, api)
        run_id = str(run.id)
        await activities.prepare(run_id)
        await activities.draft(run_id)
        await activities.request_review(run_id)
        await activities.record_approval(run_id)
        key = (PROJECT_ID, GITHUB_USER_PROVIDER)
        stored = database.integrations_db.connections[key]
        database.integrations_db.connections[key] = type(stored)(
            **{**stored.__dict__, "external_account_id": "9999"}
        )
        with pytest.raises(ApplicationError, match="different GitHub account"):
            await activities.apply(run_id)
    assert api.pulls == [] and api.issues == []


async def test_an_unusable_report_stops_before_any_github_call(monkeypatch) -> None:
    api = GitHub()
    bad = {
        **PACKETS,
        "submissions": [{**PACKETS["submissions"][0], "entry": "- [X](https://x.dev) - no."}],
    }
    activities, database, run, client, service, _ = await submission_setup(
        api, monkeypatch, packets=bad
    )
    async with client:
        await connect(service, api)
        calls = len(api.requests)
        with pytest.raises(ApplicationError, match="does not link the product"):
            await activities.prepare(str(run.id))
    assert len(api.requests) == calls


def test_the_skill_example_block_is_what_the_submitter_accepts() -> None:
    skill = (
        Path(__file__).parents[1]
        / "workflow_packages/outreach.awesome_lists/skills/awesome-lists/SKILL.md"
    ).read_text()
    packets = aw.parse_packets(skill)
    assert packets["submissions"][0]["list"] == "owner/awesome-thing"
    # A plausible but unusable packet: the entry names another site, so nothing is sent.
    unusable = skill.replace("https://github.com/acme/acme) -", "https://acme.example) -")
    refused = aw.parse_packets(unusable)
    assert refused["submissions"] == []
    assert "does not link the product" in refused["rejected"][0]["reason"]


# ---------------------------------------------------------------- Postgres contract


async def _submit_run(db, *, approved: bool):
    project = await db.create_project(name="Awesome", state_repo_id=f"projects/{uuid4()}")
    run_id = uuid4()
    workflow_id = await db.pool.fetchval("SELECT id FROM workflows WHERE key=$1", aw.KEY)
    if workflow_id is None:
        workflow_id = uuid4()
        await db.pool.execute(
            """INSERT INTO workflows (id, key, title, executor, definition_repo_id,
                    definition_path, current_commit_sha, version_label, definition)
               VALUES ($1, $2, 'Submit', $2, 'registry/workflows', 'awesome.json', $3, '1',
                       '{}')""",
            workflow_id,
            aw.KEY,
            "d" * 40,
        )
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            review_required, review_decision, reviewed_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,1,1,'running',false,true,$8,
                   CASE WHEN $8::text IS NULL THEN NULL ELSE now() END)""",
        run_id,
        project.id,
        workflow_id,
        aw.KEY,
        "d" * 40,
        f"{aw.KEY}:{run_id}",
        str(run_id),
        "approved" if approved else None,
    )
    return project, run_id


async def _project(db, run_id):
    key = f"awesome_submit:{run_id}:projection"
    async with db.effect_lock(key, aw.KEY) as (conn, _):
        await db.start_effect(conn, execution_key=key, operation=aw.KEY)
        await db.complete_awesome_submit_projection(
            conn,
            execution_key=key,
            run_id=run_id,
            canonical_commit_sha="e" * 40,
            artifact_path="reports/awesome-submissions/x/RESULT.md",
            artifact_ref="code.storage://state@" + "e" * 40 + "/RESULT.md",
            summary="Sent 1 of 1 awesome list submissions from your GitHub account.",
        )


async def test_postgres_accepts_the_provider_and_finishes_only_approved_runs(publication_db):
    from tin_lite.db import SideEffectConflictError

    db = publication_db
    project, run_id = await _submit_run(db, approved=True)
    connection = await db.upsert_integration_connection(
        project_id=project.id,
        provider_key=GITHUB_USER_PROVIDER,
        external_account_id="4242",
        external_account_label="@founder",
        configuration={"login": "founder"},
        credential_ciphertext=b"sealed",
        credential_key_version="v1",
        connected_by_clerk_user_id=USER_ID,
    )
    assert connection.provider_key == GITHUB_USER_PROVIDER
    await _project(db, run_id)
    row = await db.pool.fetchrow(
        "SELECT status, result_summary FROM workflow_runs WHERE id=$1", run_id
    )
    assert row["status"] == "succeeded" and row["result_summary"].startswith("Sent 1 of 1")

    _, pending = await _submit_run(db, approved=False)
    with pytest.raises(SideEffectConflictError):
        await _project(db, pending)
    assert (
        await db.pool.fetchval("SELECT status FROM workflow_runs WHERE id=$1", pending) == "running"
    )


NUMBERED_LIST = """# AI Tools for Photo Editing

A collection of tools.

1. [Remove.bg](https://www.remove.bg) - Remove backgrounds.
2. [Prisma](https://prisma-ai.com) - Turn photos into art.

---

# Other Tools

## Video

1. [Other](https://other.dev) - Something else.
2. [More](https://more.dev) - More.
3. [Last](https://last.dev) - Last.
"""
TABLE_LIST = """# AI Image Editor Tools

| Tool Name | Description | Website |
|-----------|-------------|---------|
| Blur Background | Blur backgrounds | [https://blur.vip](https://blur.vip) |
| Cutout Pro | Cutouts | [https://cutout.pro](https://cutout.pro) |

## More Resources
- [Back](https://example.com)
"""


def test_numbered_lists_take_the_next_number_inside_their_own_section() -> None:
    links = ["https://acme.dev"]
    placed = aw.place(
        NUMBERED_LIST,
        section="# AI Tools for Photo Editing",
        entry="99. [Acme](https://acme.dev) - Replace text in images.",
        order="alphabetical",
        links=links,
    )
    assert placed["entry"] == "3. [Acme](https://acme.dev) - Replace text in images."
    lines = placed["content"].splitlines()
    assert lines[lines.index(placed["entry"]) - 1].startswith("2. [Prisma]")
    assert placed["content"].count("3. [") == 2  # the next section's own item 3 is untouched


def test_table_rows_must_match_the_header_columns() -> None:
    links = ["https://acme.dev"]
    row = "| Acme | Replace text in images | [https://acme.dev](https://acme.dev) |"
    placed = aw.place(
        TABLE_LIST, section="# AI Image Editor Tools", entry=row, order="end", links=links
    )
    lines = placed["content"].splitlines()
    assert lines[lines.index(row) - 1].startswith("| Cutout Pro")
    alphabetical = aw.place(
        TABLE_LIST, section="# AI Image Editor Tools", entry=row, order="alphabetical", links=links
    )["content"].splitlines()
    assert alphabetical[alphabetical.index(row) + 1].startswith("| Blur Background")
    with pytest.raises(aw.PacketError, match="3 columns"):
        aw.place(
            TABLE_LIST,
            section="# AI Image Editor Tools",
            entry="| Acme | [https://acme.dev](https://acme.dev) |",
            order="end",
            links=links,
        )


def test_one_unusable_packet_is_skipped_and_the_rest_still_go() -> None:
    bare_row = {
        **PACKETS["submissions"][0],
        "list": "someone/awesome-table",
        "entry": "Acme | Replace text | https://acme.dev",
    }
    packets = aw.parse_packets(
        report({**PACKETS, "submissions": [bare_row, *PACKETS["submissions"]]})
    )
    assert [s["list"] for s in packets["submissions"]] == [LIST, "other/awesome-things"]
    assert packets["rejected"][0]["list"] == "someone/awesome-table"
    assert "written like its neighbours" in packets["rejected"][0]["reason"]
