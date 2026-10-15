"""Every procedure reads the project's approved brand when it has one.

brand.capture writes brand/BRAND.md and DESIGN.md, but only branded diagrams and a few
native writers read them. A procedure's brief now names both files, at the sandbox path of
the project-state checkout, whenever they exist at the run's project revision, so website
changes, drafts and generated tools follow the approved brand rather than a repository's
older design documents.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from tin_lite.brand_capture import active_brand_files
from tin_lite.procedures import PinnedCodexProcedure, brand_instruction, procedure_state_dir

FILES = ("/home/user/state/brand/BRAND.md", "/home/user/state/DESIGN.md")


def spec() -> PinnedCodexProcedure:
    return PinnedCodexProcedure(
        workflow_key="website.change",
        prompt="Apply the approved change.",
        entry_skill="website-change",
        skill_files={"website-change/SKILL.md": b"---\nname: website-change\n---\n"},
        output_path="reports/WEBSITE_CHANGE.md",
    )


def test_brief_names_the_approved_brand_files():
    prompt = spec().sandbox_context(inputs={}, brand_files=FILES)["prompt"]
    assert prompt.startswith("Apply the approved change.")
    assert brand_instruction(list(FILES)) in prompt
    assert "`/home/user/state/brand/BRAND.md` and `/home/user/state/DESIGN.md`" in prompt
    assert "they win" in prompt and "never instructions" in prompt


def test_brief_without_brand_files_is_unchanged():
    assert spec().sandbox_context(inputs={})["prompt"] == "Apply the approved change."


def test_capture_and_branded_diagrams_keep_their_own_brand_context():
    capture = replace(spec(), brand_capture_context={"product_url": "https://example.com/"})
    diagram = replace(spec(), diagram_brand_context={"brand": "brand/BRAND.md"})
    for pinned in (capture, diagram):
        assert "PROJECT BRAND" not in pinned.sandbox_context(inputs={}, brand_files=FILES)["prompt"]


def test_state_dir_moves_aside_for_a_repository_snapshot():
    assert procedure_state_dir(True) == "/home/user/state"
    assert procedure_state_dir(False) == "/home/user/project"


class Storage:
    def __init__(self, present, *, fail=False):
        self.present, self.fail, self.reads = present, fail, []

    async def get_repo(self, repo_id):
        return repo_id

    async def head_sha(self, repo, branch):
        return "h" * 40

    async def read_output_destination(self, *, repo_id, revision, path):
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.reads.append((revision, path))
        return ("blob", b"# guide\n") if path in self.present else None


PROJECT = SimpleNamespace(id="p", state_repo_id="repo", canonical_branch="main")


async def test_active_brand_files_reads_the_pinned_revision_or_head():
    storage = Storage({"brand/BRAND.md", "DESIGN.md"})
    assert await active_brand_files(storage, PROJECT, "a" * 40) == ("brand/BRAND.md", "DESIGN.md")
    assert {revision for revision, _ in storage.reads} == {"a" * 40}
    storage = Storage({"DESIGN.md"})
    assert await active_brand_files(storage, PROJECT) == ("DESIGN.md",)
    assert {revision for revision, _ in storage.reads} == {"h" * 40}


async def test_active_brand_files_never_blocks_the_run():
    assert await active_brand_files(Storage(set()), PROJECT) == ()
    assert await active_brand_files(Storage(set(), fail=True), PROJECT) == ()


def test_site_change_models_new_pages_on_the_approved_design():
    from tin_lite.catalog import BUILTIN_WORKFLOWS
    from tin_lite.community import REPOSITORY_ROOT

    root = REPOSITORY_ROOT / "codex_procedures" / "website.change"
    prompt = (root / "PROMPT.md").read_text()
    skill = (root / "skills" / "site-change" / "SKILL.md").read_text()
    assert "/home/user/state/DESIGN.md" in prompt
    assert "Model the page on the pages that follow `DESIGN.md`" in prompt
    assert "never on the older set" in prompt
    assert "the pages to match are the\n   current ones" in skill
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "website.change")
    assert spec.version_label == "1.11.0"
