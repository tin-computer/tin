"""Pure metadata-only checks for site repairs."""

import re
from html import unescape
from html.parser import HTMLParser

from tin_lite.technical_title_rules import has_title, verify_title_change

MAX_HTML_BYTES = 250_000
TITLE_CHECK = "metadata.title_missing"
DESCRIPTION_CHECK = "metadata.description_missing"
SUPPORTED_CHECKS = frozenset({TITLE_CHECK, DESCRIPTION_CHECK})


class MetadataParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.offsets = [0]
        for line in html.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.heads, self.ends, self.in_head = 0, 0, False
        self.descriptions = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.heads += 1
            self.in_head = True
        if tag != "meta" or not any(
            key == "name" and (value or "").lower() == "description" for key, value in attrs
        ):
            return
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        self.descriptions.append(
            (start, start + len(self.get_starttag_text()), attrs, self.in_head)
        )

    def handle_endtag(self, tag):
        if tag == "head":
            self.ends += 1
            self.in_head = False


def has_metadata(html, check):
    if check == TITLE_CHECK:
        return has_title(html)
    if check != DESCRIPTION_CHECK:
        raise ValueError("Unsupported metadata check.")
    parsed = MetadataParser(html)
    if len(parsed.descriptions) != 1:
        return False
    _, _, attrs, inside = parsed.descriptions[0]
    return inside and bool((dict(attrs).get("content") or "").strip())


def verify_metadata_change(before, after, check):
    if check == TITLE_CHECK:
        return verify_title_change(before, after)
    if check != DESCRIPTION_CHECK or len(after.encode()) > MAX_HTML_BYTES:
        raise ValueError("Unsupported metadata change.")
    original, proposed = MetadataParser(before), MetadataParser(after)
    if (
        original.heads != 1
        or original.ends != 1
        or proposed.heads != 1
        or proposed.ends != 1
        or original.in_head
        or proposed.in_head
        or len(original.descriptions) > 1
        or len(proposed.descriptions) != 1
        or has_metadata(before, check)
    ):
        raise ValueError("The missing-description before/after check failed.")
    for parsed in (original, proposed):
        for _, _, attrs, inside in parsed.descriptions:
            allowed = (
                [["content", "name"]] if parsed is proposed else [["name"], ["content", "name"]]
            )
            if not inside or sorted(key for key, _ in attrs) not in allowed:
                raise ValueError("A description may contain only its name and content attributes.")
    start, end, attrs, _ = proposed.descriptions[0]
    description = dict(attrs)["content"] or ""
    if (
        not description.strip()
        or len(description) > 320
        or any(ord(char) < 32 for char in description)
        or re.search(r"[<>{}]", unescape(description))
    ):
        raise ValueError("Use one bounded, plain-text meta description.")
    old = before
    if original.descriptions:
        first, last, _, _ = original.descriptions[0]
        old = before[:first] + before[last:]
    if old != after[:start] + after[end:]:
        raise ValueError("The repair must change only the selected meta description.")
