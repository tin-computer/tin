"""The description rule website.change's site-fix-v5 checks a repaired page against."""

import pytest

from tin_lite.technical_metadata_rules import (
    DESCRIPTION_CHECK,
    has_metadata,
    verify_metadata_change,
)

BEFORE = "<!doctype html><html><head></head><body><h1>Useful page</h1></body></html>"
DESCRIPTION = '<meta name="description" content="A useful page for this example.">'
AFTER = BEFORE.replace("<head>", "<head>" + DESCRIPTION)


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
