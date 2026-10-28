"""managed.github: the public GitHub account behind a new person's email.

Provider answers are synthetic fixtures shaped like GitHub's REST responses, served through an
httpx MockTransport. No test calls the live API.
"""

from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from tin_lite import github_people, managed_services
from tin_lite.connection_records import ServiceArgumentError
from tin_lite.integrations import ServiceCallRefused
from tin_lite.managed_services import GITHUB_PROVIDER, ManagedServices, request_for

TOKEN = "fixture-github-token"  # noqa: S105


def commit(login, owner, name):
    return {
        "author": {"login": login} if login else None,
        "commit": {"author": {"name": name, "email": "ana@gmail.com"}},
        "repository": {"owner": {"login": owner}},
    }


USERS = {
    "ana-dev": {
        "login": "ana-dev",
        "html_url": "https://github.com/ana-dev",
        "name": "Ana Souza",
        "company": "@fieldnotes",
        "blog": "fieldnotes.dev",
        "bio": "Founder of Fieldnotes",
        "location": "Lisbon",
        "email": "ana@gmail.com",
        "twitter_username": "ana",
        "followers": 120,
        "public_repos": 14,
    },
    "ana-alt": {"login": "ana-alt", "html_url": "https://github.com/ana-alt", "name": "A"},
    "acme": {"login": "acme", "html_url": "https://github.com/acme", "name": "Acme Inc"},
    "magic-tps": {
        "login": "magic-tps",
        "html_url": "https://github.com/magic-tps",
        "name": "Tomás Pastor Salazar",
        "public_repos": 42,
    },
    "tomaspastor": {"login": "tomaspastor", "name": "Tomas Pastor"},
}
SOCIAL = [
    {"provider": "twitter", "url": "https://x.com/ana"},
    {"provider": "linkedin", "url": "https://www.linkedin.com/in/ana-souza/"},
    {"provider": "generic", "url": "https://wa.me/351900000000"},
    {"provider": "generic", "url": "https://fieldnotes.dev/team"},
]


def github(*, commits=(), users=(), status=None, seen=None, settings=None):
    def answer(request):
        if seen is not None:
            seen.append(request)
        if status is not None:
            return httpx.Response(status, json={"message": "no"}, headers=status_headers(status))
        path = request.url.path
        if path == "/search/commits":
            return httpx.Response(200, json={"total_count": len(commits), "items": list(commits)})
        if path == "/search/users":
            return httpx.Response(200, json={"items": [{"login": u} for u in users]})
        if path.endswith("/social_accounts"):
            return httpx.Response(200, json=SOCIAL)
        login = path.removeprefix("/users/")
        if login in USERS:
            return httpx.Response(200, json=USERS[login])
        return httpx.Response(404, json={"message": "Not Found"})

    settings = settings or SimpleNamespace(github_lookup_token=SecretStr(TOKEN))
    return ManagedServices(settings, transport=httpx.MockTransport(answer))


def status_headers(status):
    return {"x-ratelimit-remaining": "0"} if status == 403 else {}


async def lookup(service, email, name=None):
    arguments = {"email": email, **({"name": name} if name else {})}
    return await service.call(
        GITHUB_PROVIDER,
        "people.lookup",
        arguments,
        execution_key="run:step",
        max_response_bytes=20000,
    )


def test_github_is_a_free_managed_service_that_works_without_a_token():
    assert managed_services.is_managed(GITHUB_PROVIDER)
    assert not managed_services.paid(GITHUB_PROVIDER)
    assert managed_services.configured(SimpleNamespace(), GITHUB_PROVIDER)
    assert managed_services.DEFINITIONS[GITHUB_PROVIDER].unlocks == ("Codex procedures",)


def test_procedures_may_bind_github():
    from test_procedure_services import definition as procedure_definition
    from test_procedure_services import manifest as procedure_manifest

    from tin_lite.private_workflows import validate_private_definition

    value = procedure_manifest()
    value["definition"]["integration_requirements"] = [
        {"provider_key": GITHUB_PROVIDER, "capabilities": ["people.read"], "required": True}
    ]
    value["definition"]["procedure"]["services"] = {
        "github": {"provider_key": GITHUB_PROVIDER, "max_calls": 10, "max_response_bytes": 20000}
    }
    spec = validate_private_definition(procedure_definition(value))
    assert spec.services[0].provider_key == GITHUB_PROVIDER


@pytest.mark.parametrize(
    "arguments",
    [{}, {"email": "not an email"}, {"email": "a@b.co", "name": ""}, {"email": "a@b.co", "x": 1}],
)
def test_arguments_are_one_email_and_an_optional_name(arguments):
    with pytest.raises(ServiceArgumentError):
        request_for("people.lookup", arguments)


def test_handles_come_from_the_local_part_with_and_without_trailing_digits():
    assert github_people.handles("Magic.TPS972+tin@gmail.com") == ["magictps972", "magictps"]
    assert github_people.handles("ab1@gmail.com") == []


def test_a_name_ties_only_with_every_word_and_at_least_two():
    assert github_people.same_person("Tomas Pastor", "Tomás Pastor Salazar")
    assert not github_people.same_person("Tomas Pastor", "Tomas Smith")
    assert not github_people.same_person("Euloge", "Euloge Houessou")


async def test_an_exact_email_commit_names_the_account_with_work_facts_only():
    seen = []
    service = github(commits=[commit("ana-dev", "ana-dev", "Ana Souza")] * 3, seen=seen)
    result = await lookup(service, "Ana@Gmail.com")
    assert result["status"] == "found"
    assert result["commit_names"] == ["Ana Souza"]
    [account] = result["accounts"]
    assert account == {
        "login": "ana-dev",
        "url": "https://github.com/ana-dev",
        "name": "Ana Souza",
        "company": "@fieldnotes",
        "site": "https://fieldnotes.dev",
        "bio": "Founder of Fieldnotes",
        "linkedin_url": "https://www.linkedin.com/in/ana-souza/",
        "links": ["https://fieldnotes.dev/team"],
        "public_repos": 14,
        "tie": "exact_email",
        "commits": 3,
    }
    assert seen[0].url.params["q"] == "author-email:ana@gmail.com"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_the_owner_of_the_repos_counts_only_when_it_carries_the_signed_name():
    commits = [commit("ana-alt", "ana-dev", "Ana Souza"), commit("ana-alt", "acme", "Ana Souza")]
    result = await lookup(github(commits=commits), "ana@gmail.com")
    ties = [(a["login"], a["tie"]) for a in result["accounts"]]
    assert ties == [("ana-alt", "exact_email"), ("ana-dev", "email_commits_in_own_repos")]


async def test_without_commits_a_handle_ties_only_with_the_full_name():
    users = ["tomaspastor", "magic-tps", "magictps-fan"]
    result = await lookup(github(users=users), "magictps972@gmail.com", "Tomas Pastor")
    assert [(a["login"], a["tie"]) for a in result["accounts"]] == [
        ("magic-tps", "handle_and_name")
    ]
    assert result["accounts"][0]["name"] == "Tomás Pastor Salazar"


async def test_a_name_alone_or_a_handle_alone_finds_nobody():
    assert (await lookup(github(users=["tomaspastor"]), "magictps972@gmail.com", "Tomas Pastor"))[
        "status"
    ] == "not_found"
    seen = []
    result = await lookup(github(users=["magic-tps"], seen=seen), "magictps972@gmail.com")
    assert result == {"email": "magictps972@gmail.com", "status": "not_found"}
    assert [r.url.path for r in seen] == ["/search/commits"]


async def test_a_rate_limit_is_an_answer_and_a_bad_token_is_refused():
    for status in (403, 429):
        result = await lookup(github(status=status), "ana@gmail.com")
        assert result == {
            "email": "ana@gmail.com",
            "status": "unavailable",
            "reason": "rate_limited",
        }
    with pytest.raises(ServiceCallRefused) as refused:
        await lookup(github(status=401), "ana@gmail.com")
    assert refused.value.code == "provider_refused"
    assert TOKEN not in str(refused.value)


async def test_without_a_token_the_reads_are_anonymous():
    seen = []
    service = github(
        commits=[commit("ana-dev", "ana-dev", "Ana Souza")], seen=seen, settings=SimpleNamespace()
    )
    assert (await lookup(service, "ana@gmail.com"))["status"] == "found"
    assert all("authorization" not in r.headers for r in seen)
