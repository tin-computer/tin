import gzip
import hashlib
import io
import runpy
import subprocess
import tarfile
import tomllib
from copy import deepcopy
from pathlib import Path

import pytest
from test_technical_title_repair import BEFORE, archive, spec

from tin_lite import technical_fix
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.procedures import validate_codex_procedure_definition
from tin_lite.technical_build_profile import WHEEL, match_profile, match_render, wheel_package
from tin_lite.technical_metadata_rules import (
    DESCRIPTION_CHECK,
    has_metadata,
    verify_metadata_change,
)

DESCRIPTION = '<meta name="description" content="A useful page for this example.">'
AFTER = BEFORE.replace("<head>", "<head>" + DESCRIPTION)
ROOT = Path(__file__).parents[1]
MANIFEST = """[project]
name = "example"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/example"]
"""


def page(html):
    return {"html": html, "sha256": hashlib.sha256(html.encode()).hexdigest()}


@pytest.mark.parametrize(
    "before",
    [
        BEFORE,
        BEFORE.replace("<head>", '<head><meta name="description">'),
        BEFORE.replace("<head>", '<head><meta name="description" content>'),
    ],
)
def test_description_only(before):
    assert not has_metadata(before, DESCRIPTION_CHECK)
    verify_metadata_change(before, AFTER, DESCRIPTION_CHECK)
    assert has_metadata(AFTER, DESCRIPTION_CHECK)


@pytest.mark.parametrize(
    "after",
    [
        BEFORE,
        AFTER + "\n",
        AFTER.replace("Useful page", "Spam"),
        AFTER.replace(DESCRIPTION, DESCRIPTION * 2),
        AFTER.replace("<head>" + DESCRIPTION, "<head>").replace("</body>", DESCRIPTION + "</body>"),
        AFTER.replace('content="A useful page for this example."', 'content=""'),
        AFTER.replace('content="A useful page for this example."', 'content="' + "x" * 321 + '"'),
        AFTER.replace('content="A useful page for this example."', 'content="&lt;script&gt;"'),
        AFTER.replace("<meta ", '<meta onclick="bad()" '),
        AFTER.replace("<meta ", '<meta content="duplicate" '),
    ],
)
def test_unsafe_or_unrelated_changes_fail(after):
    with pytest.raises(ValueError):
        verify_metadata_change(BEFORE, after, DESCRIPTION_CHECK)


def test_cannot_rewrite_existing_description():
    with pytest.raises(ValueError):
        verify_metadata_change(AFTER, AFTER.replace("useful", "better"), DESCRIPTION_CHECK)


def test_current_and_historical_policy_contracts():
    current = deepcopy(
        next(row.definition for row in BUILTIN_WORKFLOWS if row.key == technical_fix.KEY)
    )
    assert validate_codex_procedure_definition(current).repair_policy == technical_fix.BATCH_POLICY
    assert current["procedure"]["verification"]["commands"] == []
    assert current["procedure"]["output"]["max_files"] == 20
    assert spec().repair_policy == technical_fix.POLICY
    # Earlier policies keep their three-file limit; only the batch policy may go to twenty.
    site = deepcopy(current)
    site["procedure"]["output"]["repair_policy"] = technical_fix.SITE_POLICY
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(site)
    site["procedure"]["output"]["max_files"] = 3
    assert validate_codex_procedure_definition(site).repair_policy == technical_fix.SITE_POLICY
    current["procedure"]["output"]["max_files"] = 3
    metadata = deepcopy(current)
    metadata["procedure"]["output"]["repair_policy"] = technical_fix.POLICY
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(metadata)  # v3 still needs its sandbox verifier.
    metadata["procedure"]["verification"]["commands"] = [technical_fix.CHECK_COMMAND]
    assert validate_codex_procedure_definition(metadata).repair_policy == technical_fix.POLICY
    previous = deepcopy(current)
    previous["procedure"]["verification"]["commands"] = [technical_fix.CHECK_COMMAND]
    previous["procedure"]["output"]["repair_policy"] = technical_fix.WHOLE_FINDING_POLICY
    assert (
        validate_codex_procedure_definition(previous).repair_policy
        == technical_fix.WHOLE_FINDING_POLICY
    )
    old = deepcopy(current)
    old["procedure"]["output"]["repair_policy"] = technical_fix.LEGACY_POLICY
    old["procedure"]["verification"]["commands"] = [technical_fix.LEGACY_CHECK_COMMAND]
    assert validate_codex_procedure_definition(old).repair_policy == technical_fix.LEGACY_POLICY
    assert DESCRIPTION_CHECK not in technical_fix.supported_checks(technical_fix.LEGACY_POLICY)
    old["procedure"]["verification"]["commands"] = [technical_fix.CHECK_COMMAND]
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(old)


def test_full_template_matching_and_ambiguity():
    template = BEFORE.replace("</head>", '<link href="/app.css?v={{ASSET_VERSION}}"></head>')
    html = template.replace("{{ASSET_VERSION}}", "abc123")
    files = {"pyproject.toml": MANIFEST, "src/example/index.html": template}
    result = match_profile(archive(files), [page(html)], DESCRIPTION_CHECK)
    assert result["verification_profile"]["kind"] == WHEEL
    assert result["originals"] == {"src/example/index.html": template}
    assert not match_profile(archive(files), [page(html + " ")], DESCRIPTION_CHECK)
    files["src/example/duplicate.html"] = template
    assert not match_profile(archive(files), [page(html)], DESCRIPTION_CHECK)
    assert match_render("{{AUTH_RETURN_URL}}:{{AUTH_FLOW}}", ":product") == {
        "AUTH_RETURN_URL": "",
        "AUTH_FLOW": "product",
    }
    assert (
        match_render("{{AUTH_RETURN_URL}}", "https://clerk.test/oauth/authorize?state=private")
        is None
    )
    assert match_render("{{AUTH_FLOW}}", "mcp") is None
    assert match_render("{{UNKNOWN}}", "value") is None
    assert match_render("{{ASSET_VERSION}}:{{ASSET_VERSION}}", "a:b") is None


def test_one_generated_page_does_not_hide_a_supported_source():
    raw = archive({"pyproject.toml": MANIFEST, "src/example/index.html": BEFORE})
    pages = [
        {**page(BEFORE), "url": "https://example.com/"},
        {
            **page("<html><head></head><body>Generated API docs</body></html>"),
            "url": "https://example.com/docs",
        },
    ]
    assert match_profile(raw, pages, DESCRIPTION_CHECK) is None  # v2 contract remains all-or-none.
    partial = match_profile(raw, pages, DESCRIPTION_CHECK, allow_partial=True)
    assert partial["originals"] == {"src/example/index.html": BEFORE}
    assert partial["unsupported_pages"] == [
        {"url": "https://example.com/docs", "reason": "No supported source HTML match"}
    ]
    assert [p["url"] for p in partial["verification_profile"]["observations"]] == [
        "https://example.com/"
    ]


@pytest.mark.parametrize(
    "addition",
    [
        "[tool.hatch.build.hooks.custom]\n",
        "[tool.hatch.metadata]\n",
        '[tool.hatch.version]\npath="version.py"\n',
    ],
)
def test_build_hooks_and_plugins_fail_closed(addition):
    with pytest.raises(ValueError):
        wheel_package((MANIFEST + addition).encode())


def test_alternate_hatch_configuration_and_traversal_rejected():
    assert not match_profile(
        archive({"pyproject.toml": MANIFEST, "hatch.toml": "", "src/example/index.html": BEFORE}),
        [page(BEFORE)],
        DESCRIPTION_CHECK,
    )
    assert not match_profile(archive({"../index.html": BEFORE}), [page(BEFORE)], DESCRIPTION_CHECK)
    with pytest.raises(ValueError):
        wheel_package(
            MANIFEST.replace(
                'version = "0.1.0"', 'version = "0.1.0"\nreadme="../README.md"'
            ).encode()
        )


def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "src/tin_lite"))
    return runpy.run_path(str(ROOT / "sandbox/verify_technical_metadata.py"))


def test_static_trusted_verifier_and_snapshot_tampering(monkeypatch):
    raw = archive({"index.html": BEFORE})
    prepared = {
        **match_profile(raw, [page(BEFORE)], DESCRIPTION_CHECK),
        "selection": {"finding": {"check_id": DESCRIPTION_CHECK}},
    }
    verify = verifier(monkeypatch)["verify"]
    verify(prepared, raw, {"index.html": AFTER})
    verify(prepared, raw, {"index.html": BEFORE}, no_change=True)
    with pytest.raises(ValueError):
        verify(prepared, archive({"index.html": BEFORE + "x"}), {"index.html": AFTER})
    verify(prepared, archive({"index.html": BEFORE}), {"index.html": AFTER})
    repacked = gzip.compress(gzip.decompress(raw), mtime=123)
    assert repacked != raw
    verify(prepared, repacked, {"index.html": AFTER})
    with pytest.raises(ValueError):
        verify(prepared, raw, {"index.html": AFTER}, no_change=True)


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_real_tin_template_profile_and_offline_wheel(tmp_path, monkeypatch, enabled):
    # Opt-in build test uses the exact pinned checker environment; ordinary unit
    # runs still exercise source matching against the real committed repository.
    raw = subprocess.check_output(["git", "archive", "--format=tar.gz", "HEAD"], cwd=ROOT)  # noqa: S603,S607
    # Compare the pinned archive to its own template, not unrelated unstaged UI edits.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
        template = source.extractfile("src/tin_lite/static/index.html").read().decode()
        metadata = source.extractfile("pyproject.toml").read()
    html = (
        template.replace("{{ASSET_VERSION}}", "public-build")
        .replace("{{CLERK_PUBLISHABLE_KEY}}", "public-key")
        .replace("{{CLERK_FRONTEND_API_URL}}", "https://public.example")
        .replace("{{BILLING_ENABLED}}", enabled)
        .replace("{{AUTH_RETURN_URL}}", "")
        .replace("{{AUTH_FLOW}}", "product")
    )
    matched = match_profile(raw, [page(html)], DESCRIPTION_CHECK)
    if tomllib.loads(metadata.decode())["project"].get("license-files"):
        # The repository now packages third-party license globs. The pinned
        # offline repair profile deliberately rejects those; do not silently
        # broaden its build contract (or claim this is a supported live repair).
        with pytest.raises(ValueError, match="Custom license globs"):
            wheel_package(metadata)
        assert matched is None
        return
    assert matched and matched["verification_profile"]["kind"] == WHEEL
    # Reuse the test's Python when hatchling is available (CI needn't install build tooling).
    import importlib.util

    if importlib.util.find_spec("hatchling") is None:
        return
    changes = {
        path: value.replace("<head>", "<head>" + DESCRIPTION)
        for path, value in matched["originals"].items()
    }
    prepared = {**matched, "selection": {"finding": {"check_id": DESCRIPTION_CHECK}}}
    verifier(monkeypatch)["verify"](prepared, raw, changes)
