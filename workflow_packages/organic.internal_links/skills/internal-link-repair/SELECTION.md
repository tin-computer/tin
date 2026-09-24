# Anchor selection

This resource owns where a link may go. Extract the single Python block below into one scratch
module and use it unchanged. It reads text you pass it and returns spans; it opens no files,
makes no requests, and writes nothing.

The rule it enforces is the reason this workflow is safe to merge: a link may only wrap wording
that is **already** in the page's prose. Code never invents a sentence, so a reviewer is reading
a diff that adds link syntax around bytes that were already there.

Prose excludes frontmatter, headings, fenced and inline code, existing links and their text, HTML
tags and attributes, MDX imports and JSX expressions, script and style bodies, and bare URLs.
A phrase that only occurs inside one of those is not an anchor, and the page is not a candidate.

`rank_candidates` returns at most `link_budget` entries, each naming a file, a byte span, the
exact matched text and a reason. Ties break on path, so two runs over the same repository choose
the same files. It is a filter, not a judgment: you still decide whether each proposed link is
one a reader would actually want to follow, and you drop the ones that are not.

```python
"""Locate honest internal-link anchors in Markdown/MDX and static HTML. Standard library only."""

import re

MARKDOWN = "markdown"
HTML = "html"

_MARKDOWN_MASKS = (
    re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S),  # YAML frontmatter
    re.compile(r"(?ms)^[ \t]{0,3}(?P<f>`{3,}|~{3,}).*?^[ \t]{0,3}(?P=f)[ \t]*$"),  # fenced code
    re.compile(r"`[^`\n]*`"),  # inline code
    re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)"),  # inline links and images
    re.compile(r"\[[^\]\n]*\]\[[^\]\n]*\]"),  # reference links
    re.compile(r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:.*$"),  # link definitions
    re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t].*$"),  # ATX headings
    re.compile(r"(?m)^[ \t]*(?:import|export)\b.*$"),  # MDX module syntax
    re.compile(r"<a\b[^>]*>.*?</a\s*>", re.S | re.I),  # embedded anchors
    re.compile(r"<[^>\n]{0,400}>"),  # embedded tags and autolinks
    re.compile(r"\{[^{}\n]*\}"),  # JSX expressions
    re.compile(r"https?://\S+"),  # bare URLs
)

_HTML_MASKS = (
    re.compile(r"<!--.*?-->", re.S),
    re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I),
    re.compile(r"<a\b[^>]*>.*?</a\s*>", re.S | re.I),
    re.compile(r"<(h[1-6])\b[^>]*>.*?</\1\s*>", re.S | re.I),
    re.compile(r"<[^>]*>", re.S),
)

_WORD = re.compile(r"[0-9A-Za-z_]")


def prose_mask(text, kind=MARKDOWN):
    """True where a character is ordinary prose a link may wrap."""
    mask = [True] * len(text)
    for pattern in _HTML_MASKS if kind == HTML else _MARKDOWN_MASKS:
        for found in pattern.finditer(text):
            for index in range(*found.span()):
                mask[index] = False
    return mask


def _phrase_pattern(phrase):
    words = [re.escape(word) for word in phrase.split()]
    return re.compile(r"(?i)" + r"[ \t]+".join(words)) if words else None


def find_anchor(text, phrase, kind=MARKDOWN, mask=None):
    """The first prose occurrence of phrase, or None. Never spans a line break."""
    pattern = _phrase_pattern(" ".join(phrase.split()))
    if pattern is None:
        return None
    mask = prose_mask(text, kind) if mask is None else mask
    for found in pattern.finditer(text):
        start, end = found.span()
        before, after = text[start - 1 : start], text[end : end + 1]
        if _WORD.match(before) or _WORD.match(after):
            continue
        if all(mask[start:end]):
            return {"start": start, "end": end, "text": found.group(0)}
    return None


def apply_link(text, anchor, href, kind=MARKDOWN):
    """Wrap the located span in a link, preserving every other byte."""
    matched = text[anchor["start"] : anchor["end"]]
    if matched != anchor["text"]:
        raise ValueError("anchor no longer matches its source text")
    if kind == HTML:
        safe = href.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        link = f'<a href="{safe}">{matched}</a>'
    else:
        if any(character in href for character in "() "):
            raise ValueError("unsupported characters in a Markdown link target")
        link = f"[{matched}]({href})"
    return text[: anchor["start"]] + link + text[anchor["end"] :]


def outbound_links(text, kind=MARKDOWN):
    if kind == HTML:
        return len(re.findall(r"<a\b[^>]*\bhref\s*=", text, re.I))
    return len(re.findall(r"\]\([^)\n]+\)", text))


def rank_candidates(target, pages, link_budget=3, saturation=12):
    """Rank source pages that could link to target. Deterministic; ties break on path."""
    href, phrases = target["href"], [" ".join(p.split()) for p in target["phrases"]]
    phrases = sorted({p for p in phrases if p}, key=lambda p: (-len(p.split()), -len(p), p))
    candidates = []
    for page in pages:
        if page["path"] == target.get("path") or href in page["text"]:
            continue
        kind = page.get("kind", MARKDOWN)
        mask = prose_mask(page["text"], kind)
        for phrase in phrases:
            anchor = find_anchor(page["text"], phrase, kind, mask)
            if anchor is None:
                continue
            existing = outbound_links(page["text"], kind)
            crowded = existing >= saturation
            score = 3 * len(phrase.split()) + min(_count(page["text"], phrase, mask), 3)
            score -= 2 if crowded else 0
            reason = f"{page['path']} already says {phrase!r} in its own prose"
            if crowded:
                reason += f"; it carries {existing} outbound links already"
            candidates.append(
                {
                    "path": page["path"],
                    "kind": kind,
                    "phrase": phrase,
                    "anchor": anchor,
                    "score": score,
                    "outbound_links": existing,
                    "reason": reason,
                }
            )
            break
    candidates.sort(key=lambda item: (-item["score"], item["path"]))
    return candidates[:link_budget]


def _count(text, phrase, mask):
    pattern = _phrase_pattern(phrase)
    if pattern is None:
        return 0
    return sum(1 for f in pattern.finditer(text) if all(mask[f.start() : f.end()]))
```

## Using the result

Apply the edits one file at a time with `apply_link`, in the order returned. Re-read the file
after each edit; a second anchor in the same file is out of scope for this workflow, and stale
spans are a bug, not something to work around. Files the ranking never returned stay untouched,
including ones you believe ought to link to the target.
