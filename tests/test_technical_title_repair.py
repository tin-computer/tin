import io
import json
import runpy
import tarfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tin_lite import technical_fix
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.procedures import (
    PinnedCodexProcedure,
    validate_codex_procedure_definition,
    validate_procedure_pull_request,
)
from tin_lite.technical_title_rules import has_title, verify_title_change

BEFORE = "<!doctype html><html><head></head><body><h1>Useful page</h1></body></html>"
AFTER = BEFORE.replace("<head>", "<head><title>Useful page</title>")


def archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, text in files.items():
            raw = text.encode()
            item = tarfile.TarInfo(path)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    return buffer.getvalue()


def spec():
    definition = next(row for row in BUILTIN_WORKFLOWS if row.key == technical_fix.KEY).definition
    contract = validate_codex_procedure_definition(definition)
    return PinnedCodexProcedure(
        workflow_key=technical_fix.KEY,
        prompt="test",
        entry_skill="test",
        skill_files={},
        result_kind=contract.result_kind,
        workspace_kind=contract.workspace_kind,
        output_max_files=3,
        output_max_bytes=contract.output_max_bytes,
        verification_commands=contract.verification_commands,
        repair_policy=contract.repair_policy,
    )


def manifest(*, no_change=False):
    return {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": "b" * 40,
        "title": "Add page title",
        "body": "One evidenced repair.",
        "files": [] if no_change else [{"path": "index.html", "content": AFTER}],
        "outcome": "no_change" if no_change else "patch",
        "reason": "no_safe_patch" if no_change else "",
        "verification": [technical_fix.CHECK_COMMAND],
    }


def prepared():
    return {
        "repository_binding": {
            key: manifest()[key] for key in ("repository", "default_branch", "head_sha")
        },
        "originals": {"index.html": BEFORE},
    }


def test_positive_patch_proves_before_after_and_changes_nothing_else():
    assert not has_title(BEFORE)
    assert has_title(AFTER)
    verify_title_change(BEFORE, AFTER)
    validate_procedure_pull_request(json.dumps(manifest()).encode(), spec=spec())
    technical_fix.validate_manifest(manifest(), prepared())


def test_sandbox_templates_construct_with_shared_verifier_inside_build_context():
    module = runpy.run_path(str(Path(__file__).parents[1] / "sandbox/template.py"))
    for name in ("task_template", "browser_template", "studio_template"):
        assert type(module[name]()).__name__ == "TemplateBuilder"


@pytest.mark.parametrize(
    "after",
    [
        BEFORE,
        AFTER.replace("Useful page</h1>", "Sales pitch</h1>"),
        AFTER.replace("</title>", "</title>\n"),
        AFTER.replace("Useful page</title>", "</title>"),
        AFTER.replace("</head>", "<title>Duplicate</title></head>"),
        BEFORE.replace("</body>", "<title>Wrong location</title></body>"),
        AFTER.replace("Useful page</title>", "x" * 201 + "</title>"),
        AFTER.replace("Useful page</title>", "<script>bad()</script></title>"),
    ],
)
def test_rejects_empty_duplicate_misplaced_or_broader_changes(after):
    with pytest.raises(ValueError):
        verify_title_change(BEFORE, after)


def test_existing_title_is_not_a_missing_title_repair():
    with pytest.raises(ValueError):
        verify_title_change(AFTER, AFTER.replace("Useful page</title>", "SEO rewrite</title>"))
    verify_title_change(BEFORE.replace("<head>", "<head><title></title>"), AFTER)


def test_no_change_requires_opt_in_and_zero_files():
    value = manifest(no_change=True)
    validate_procedure_pull_request(json.dumps(value).encode(), spec=spec())
    technical_fix.validate_manifest(value, prepared())
    with pytest.raises(ValueError):
        validate_procedure_pull_request(
            json.dumps(value).encode(), spec=replace(spec(), repair_policy=None)
        )
    value["files"] = manifest()["files"]
    with pytest.raises(ValueError):
        validate_procedure_pull_request(json.dumps(value).encode(), spec=spec())


@pytest.mark.parametrize(
    "field,value",
    [("head_sha", "c" * 40), ("repository", "other/site"), ("default_branch", "other")],
)
def test_patch_cannot_rebind_the_target(field, value):
    proposal = manifest()
    proposal[field] = value
    with pytest.raises(ValueError):
        technical_fix.validate_manifest(proposal, prepared())


def test_patch_cannot_add_paths_or_skip_a_selected_file():
    proposal = manifest()
    proposal["files"][0]["path"] = "app.py"
    with pytest.raises(ValueError):
        technical_fix.validate_manifest(proposal, prepared())
    source = prepared()
    source["originals"]["other.html"] = BEFORE
    with pytest.raises(ValueError):
        technical_fix.validate_manifest(manifest(), source)


def test_mapping_requires_exact_unique_html_not_a_guessed_framework_route():
    pages = [{"html": BEFORE, "has_title": False}]
    assert technical_fix.matched_sources(archive({"public/index.html": BEFORE}), pages) == {
        "public/index.html": BEFORE
    }
    assert (
        technical_fix.matched_sources(archive({"index.html": BEFORE, "other.html": BEFORE}), pages)
        is None
    )
    assert technical_fix.matched_sources(archive({"index.html": BEFORE + "\n"}), pages) is None
    assert technical_fix.matched_sources(archive({"route.tsx": BEFORE}), pages) is None
    assert (
        technical_fix.matched_sources(
            archive({"index.html": BEFORE, "pyproject.toml": "[project]"}), pages
        )
        is None
    )


def resolver(ip="93.184.215.14"):
    return AsyncMock(return_value=[(2, 1, 6, "", (ip, 443))])


async def test_fresh_check_pins_public_socket_but_preserves_tls_and_http_hostname():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(200, content=BEFORE, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value = await technical_fix.fetch_page(
            "https://example.com/guide", host="example.com", resolver=resolver(), client=client
        )
    assert value["url"] == "https://example.com/guide"
    assert value["has_title"] is False
    assert len(requests) == 1


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.215.14", "2606:4700:4700::1111"],
        ["2606:4700:4700::1111", "93.184.215.14"],
    ],
)
async def test_fresh_check_keeps_resolver_preference_and_checks_every_address(addresses):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=BEFORE, headers={"content-type": "text/html"})

    rows = AsyncMock(return_value=[(2, 1, 6, "", (ip, 443)) for ip in addresses])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await technical_fix.fetch_page(
            "https://example.com/", host="example.com", resolver=rows, client=client
        )
        assert [request.url.host for request in requests] == [addresses[0]]

        rows.return_value.append((2, 1, 6, "", ("127.0.0.1", 443)))
        with pytest.raises(ValueError, match="public network"):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=rows, client=client
            )
    assert len(requests) == 1


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
async def test_private_dns_resolution_never_opens_a_socket(ip):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as client:
        with pytest.raises(ValueError, match="public network"):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=resolver(ip), client=client
            )


@pytest.mark.parametrize(
    "location", ["https://other.example/", "http://example.com/", "https://example.com:8443/"]
)
async def test_redirects_cannot_expand_the_audited_scope(location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=resolver(), client=client
            )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,body,mime",
    [
        (404, AFTER, "text/html"),
        (200, AFTER, "application/json"),
        (200, "x" * 250_001, "text/html"),
    ],
)
async def test_failed_fetch_is_not_already_resolved(status, body, mime):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=body, headers={"content-type": mime})
        )
    ) as client:
        with pytest.raises(ValueError):
            await technical_fix.fetch_page(
                "https://example.com/", host="example.com", resolver=resolver(), client=client
            )
