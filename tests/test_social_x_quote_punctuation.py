"""Editorial quote punctuation must not change the factual words being checked."""

from copy import deepcopy

import pytest
from test_social_x_compose import POST, package


@pytest.mark.parametrize(
    "text",
    [
        "The page says “Coming soon,” so the launch date is unknown.",
        'The page says "Coming soon," so the launch date is unknown.',
        "The page says “Coming soon.”",
        'The page says "Coming soon."',
        "The page says “Coming soon”.",
    ],
)
def test_quote_accepts_editorial_comma_or_period(text):
    module, _ = package()
    source = "The page says “Coming soon”; no launch date is confirmed."
    post = {**deepcopy(POST), "text": text}
    units = [{"id": "s1", "source_path": "notes", "excerpt": source}]
    result = module._validate_model({"posts": [post]}, 1, units, {"notes": source}, {})
    assert result[0]["text"] == text
    assert result[0]["readiness"] == "ready"


@pytest.mark.parametrize(
    "quote", ["Already launched,", "Coming very soon.", "coming soon,", "...."]
)
def test_quote_with_changed_words_or_only_punctuation_needs_review(quote):
    module, _ = package()
    source = "The page says “Coming soon”; no launch date is confirmed."
    post = {**deepcopy(POST), "text": f"The page says “{quote}”"}
    units = [{"id": "s1", "source_path": "notes", "excerpt": source}]
    result = module._validate_model({"posts": [post]}, 1, units, {"notes": source}, {})
    assert result[0]["text"] == post["text"]
    assert result[0]["readiness"] == "needs_evidence"
    assert "Check the quotation" in result[0]["editor_notes"]
