"""Pin changed native instructions without changing the run or executor architecture."""

from pathlib import Path

from tin_lite.skills import MAX_SKILL_SUITE_BYTES, load_skill_suite

SUITES = {
    "visibility.audit": "visibility-audit",
    "project.weekly_brief": "weekly-brief",
    "project.memory": "project-memory",
    "scan.report": "scan-report",
}
ROOT = Path(__file__).resolve().parents[2] / "workflow_skills"


def suite_for_workflow(key, *, legacy=False):
    if key not in SUITES:
        return None
    return load_skill_suite((ROOT / "legacy-pre-quality" if legacy else ROOT) / SUITES[key])


def pinned_suite(definition, key):
    if definition.get("key") != key:
        raise ValueError("Native instruction definition belongs to another workflow")
    suite = definition.get("native_skill_suite")
    if suite is None:
        return suite_for_workflow(key, legacy=True)
    if not isinstance(suite, str) or not 0 < len(suite.encode()) <= MAX_SKILL_SUITE_BYTES:
        raise ValueError("Native instruction suite exceeds its bound")
    return suite
