"""The Code map lives in one place, and its readers find it there.

product.code_map writes the `### Code map` section of project memory (`wiki/INDEX.md`, under
`## Product`). A private package read `product/code-map.md` instead, found nothing, and
reported the Code map missing. The section stays the only copy: code workflows read it with
`ctx.files.read_section`, and a read of the old path returns the same section when the
project has no such file.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_code_project_files import FileStorage
from test_procedure_publication import publication_db as publication_db
from test_product_deep_dive import CODE_MAP, FEATURE_MAP, _index
from test_workflow_code import OUTPUT, SyntheticCompute, activate_code, setup, start

from tin_lite import code_project_files, code_runner
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import MEMORY_INDEX_PATH, PRODUCT_CODE_MAP_WORKFLOW_NAME
from tin_lite.procedures import (
    CODE_MAP_SECTION,
    FEATURE_MAP_SECTION,
    MEMORY_SECTION_PARENT,
    PinnedCodexProcedure,
    memory_section_present,
    memory_section_text,
    validate_procedure_artifact,
)

ROOT = Path(__file__).parents[1]
WRITER = next(w for w in BUILTIN_WORKFLOWS if w.key == PRODUCT_CODE_MAP_WORKFLOW_NAME).procedure
PADDING = "## Architecture\n\n" + "".join(f"- Durable fact {n:05d}.\n" for n in range(3500))


def written_by_the_code_map(*sections: str, intro: str | None = None) -> bytes:
    """An index the product.code_map writer's own validator accepts, with the given sections."""
    kwargs = {} if intro is None else {"intro": intro}
    base = _index(FEATURE_MAP, **kwargs)
    content = _index(*sections, **kwargs)
    spec = PinnedCodexProcedure(
        workflow_key=PRODUCT_CODE_MAP_WORKFLOW_NAME,
        prompt="Map the product from its code.",
        entry_skill=WRITER.entry_skill,
        skill_files={"product-code-map/SKILL.md": b"---\nname: product-code-map\n---\n"},
        output_path=WRITER.output_path,
        output_validator=WRITER.output_validator,
        output_max_bytes=WRITER.output_max_bytes,
        output_section=WRITER.output_section,
    )
    validate_procedure_artifact(content, spec=spec, base=base)
    return content


def test_the_writer_and_its_readers_agree_on_one_location():
    assert WRITER.output_path == MEMORY_INDEX_PATH == "wiki/INDEX.md"
    assert WRITER.output_section.parent == MEMORY_SECTION_PARENT
    assert WRITER.output_section.heading == CODE_MAP_SECTION
    assert code_project_files.SECTION_PATHS == {"product/code-map.md": CODE_MAP_SECTION}
    # Every package that declares the Code map as a prerequisite names the same section.
    readers = 0
    for manifest in sorted(ROOT.glob("workflow_packages/*/workflow.json")):
        definition = json.loads(manifest.read_text())["definition"]
        for item in definition.get("prerequisites", []):
            if item.get("producer") == PRODUCT_CODE_MAP_WORKFLOW_NAME:
                readers += 1
                assert (item["path"], item["section"]) == (MEMORY_INDEX_PATH, CODE_MAP_SECTION)
    assert readers >= 5


def test_section_text_is_what_the_writer_bounds():
    index = written_by_the_code_map(CODE_MAP, FEATURE_MAP).decode()
    assert memory_section_text(index, CODE_MAP_SECTION) == CODE_MAP
    assert memory_section_text(index, FEATURE_MAP_SECTION) == FEATURE_MAP
    # Older projects wrote the heading without the parenthetical; it is found the same way.
    plain = CODE_MAP.replace(CODE_MAP.splitlines()[0], CODE_MAP_SECTION)
    assert memory_section_text(_index(plain).decode(), CODE_MAP_SECTION) == plain
    # Missing, outside `## Product`, or declared twice: no section, and no prerequisite either.
    for text in (
        _index(FEATURE_MAP).decode(),
        "# Memory\n\n" + CODE_MAP,
        _index(CODE_MAP, CODE_MAP).decode(),
    ):
        assert memory_section_text(text, CODE_MAP_SECTION) is None
        assert not memory_section_present(text, CODE_MAP_SECTION)


class SectionCompute(SyntheticCompute):
    """A code package that reads the Code map through the file bridge."""

    def __init__(self, reads):
        super().__init__()
        self.reads, self.results = reads, []

    async def run_code_and_kill(self, *, packet, model_call, **kwargs):
        for request in self.reads:
            try:
                encoded = await model_call({"kind": "file", **request})
            except code_project_files.CodeProjectFileError as exc:
                self.results.append(exc.code)
            else:
                self.results.append(base64.b64decode(encoded).decode("utf-8"))
        self.calls += 1
        return json.dumps({"path": OUTPUT, "content": "# Read the Code map\n"}).encode()


SECTION = {"operation": "read_section", "path": MEMORY_INDEX_PATH, "section": CODE_MAP_SECTION}
OLD_PATH = {"operation": "read_text", "path": "product/code-map.md"}
WHOLE = {"operation": "read_text", "path": MEMORY_INDEX_PATH}


async def run_reads(f, monkeypatch, files, reads):
    storage, compute = FileStorage(), SectionCompute(reads)
    server, _common, code = await setup(f, monkeypatch, storage=storage, compute=compute)
    active = await activate_code(f, server)
    storage.repo.edit(files)
    run = await start(f, server, active)
    await ActivityEnvironment().run(code.execute, run["id"])
    assert compute.calls == 1
    return compute.results


async def test_a_code_workflow_finds_what_product_code_map_wrote(billed, monkeypatch):
    index = written_by_the_code_map(CODE_MAP, FEATURE_MAP)
    results = await run_reads(
        billed,
        monkeypatch,
        {MEMORY_INDEX_PATH: index},
        [SECTION, OLD_PATH, {**OLD_PATH, "operation": "read_bytes"}],
    )
    assert results == [CODE_MAP, CODE_MAP, CODE_MAP]


async def test_a_large_index_still_yields_its_code_map(billed, monkeypatch):
    # The index may grow to 100,000 bytes; a whole-file read stops at 64,000, a section does not.
    intro = f"# Test memory\n\n{PADDING}\n"
    index = written_by_the_code_map(CODE_MAP, FEATURE_MAP, intro=intro)
    assert 64_000 < len(index) <= 100_000
    results = await run_reads(
        billed, monkeypatch, {MEMORY_INDEX_PATH: index}, [WHOLE, SECTION, OLD_PATH]
    )
    assert results == ["file_too_large_or_not_regular", CODE_MAP, CODE_MAP]


async def test_old_layouts_keep_working(billed, monkeypatch):
    own_file = b"# Code map kept by hand\n"
    no_map = _index(FEATURE_MAP)
    results = await run_reads(
        billed,
        monkeypatch,
        # A project that kept its own product/code-map.md reads that file, as before.
        {MEMORY_INDEX_PATH: no_map, "product/code-map.md": own_file},
        [OLD_PATH, SECTION],
    )
    assert results == [own_file.decode(), "file_not_found"]


async def test_missing_memory_and_bad_requests_are_named(billed, monkeypatch):
    results = await run_reads(
        billed,
        monkeypatch,
        {"notes/one.md": b"no memory yet\n"},
        [
            SECTION,
            OLD_PATH,
            {**SECTION, "path": "notes/one.md"},
            {**SECTION, "section": "Code map"},
            {"operation": "read_text", "path": MEMORY_INDEX_PATH, "section": CODE_MAP_SECTION},
        ],
    )
    assert results == [
        "file_not_found",
        "file_not_found",
        "invalid_file_request",
        "invalid_file_request",
        "invalid_file_request",
    ]


def test_authored_code_reads_a_section_by_heading(monkeypatch):
    requests, replies = (
        [],
        iter(
            [{"result": base64.b64encode(CODE_MAP.encode()).decode()}, {"error": "file_not_found"}]
        ),
    )

    class Socket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _seconds):
            pass

        def connect(self, _path):
            pass

        def sendall(self, raw):
            requests.append(json.loads(raw))

        def makefile(self, _mode):
            return io.BytesIO(json.dumps(next(replies)).encode() + b"\n")

    monkeypatch.setattr(code_runner.socket, "socket", Socket)
    files = code_runner.Context({}).files
    assert files.read_section("### Code map") == CODE_MAP
    with pytest.raises(FileNotFoundError, match="wiki/INDEX.md ### Feature map"):
        files.read_section("### Feature map")
    assert requests[0] == {
        "kind": "file",
        "operation": "read_section",
        "path": "wiki/INDEX.md",
        "section": "### Code map",
    }
