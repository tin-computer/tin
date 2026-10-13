"""Source priority and bounded-wiki regressions for X composition."""

import pytest
from test_social_x_compose import FACT, Files, inputs, package


@pytest.mark.parametrize("wiki", ["Example is a live reading practice app.", "", "   ", None])
@pytest.mark.parametrize("curated", [False, True])
def test_product_memory_precedes_historical_onboarding(wiki, curated):
    module, _ = package()
    plan_path = "reports/GROWTH_ONBOARDING_PLAN.md"
    values = {plan_path: "This side project has no public site yet."}
    if wiki is not None:
        values["wiki/INDEX.md"] = wiki
    if curated:
        values["context/product-marketing.md"] = "Example helps readers practise daily."
    files = Files(values)
    _, sources, units, *_ = module._source_packet(inputs(), files)
    expected = (
        "context/product-marketing.md"
        if curated
        else "wiki/INDEX.md"
        if wiki and wiki.strip()
        else plan_path
    )
    assert sources == {"direction": FACT, expected: values[expected]}
    assert {unit["source_path"] for unit in units} == {"direction", expected}
    if expected != plan_path:
        assert plan_path not in files.reads


@pytest.mark.parametrize(
    "wiki, expected",
    [
        ("x" * 64_000, "wiki/INDEX.md"),
        ("x" * 64_001, "reports/GROWTH_ONBOARDING_PLAN.md"),
        ("ğ" * 32_001, "reports/GROWTH_ONBOARDING_PLAN.md"),
    ],
    ids=["exact-byte-limit", "over-byte-limit", "multibyte-over-limit"],
)
def test_oversized_wiki_uses_bounded_fallback(wiki, expected):
    module, _ = package()
    files = Files(
        {
            "wiki/INDEX.md": wiki,
            "reports/GROWTH_ONBOARDING_PLAN.md": "Example is a reading practice app.",
        }
    )
    _, sources, *_ = module._source_packet(inputs(), files)
    assert sources == {"direction": FACT, expected: files.values[expected]}
