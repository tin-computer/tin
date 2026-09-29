"""Read the project's current brand notes; caller text is a missing-file fallback."""


def run(ctx, inputs):
    source = "BRAND.md"
    try:
        brand = ctx.files.read_text(source)
    except FileNotFoundError:
        brand = inputs.get("brand_notes", "")
        source = "caller-supplied brand notes"
    if not brand.strip():
        raise ValueError("Add BRAND.md or supply brand notes")
    if len(brand.encode("utf-8")) > 30_000:
        raise ValueError("Brand notes exceed this example's 30,000-byte limit")
    return {
        "path": "reports/BRAND_REFERENCE.md",
        "content": f"# Brand reference\n\nSource: {source}\n\n{brand}",
    }
