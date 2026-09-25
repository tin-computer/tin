import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_catalog_doc_matches_the_catalog():
    spec = importlib.util.spec_from_file_location(
        "dump_catalog", ROOT / "scripts" / "dump_catalog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DOC_PATH.read_text(encoding="utf-8") == module.render(), (
        "run scripts/dump_catalog.py"
    )
