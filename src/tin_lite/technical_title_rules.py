"""Dependency-free title-only checks for site repairs."""

import re
from html.parser import HTMLParser

MAX_HTML_BYTES = 250_000
TITLE = re.compile(r"<title\s*>[^<]*</title\s*>", re.IGNORECASE)


class TitleParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.titles, self.heads, self.in_title, self.current = [], 0, False, ""
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.heads += 1
        if tag == "title":
            self.in_title, self.current = True, ""

    def handle_endtag(self, tag):
        if tag == "title" and self.in_title:
            self.titles.append(self.current.strip())
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.current += data


def has_title(html):
    parsed = TitleParser(html)
    return len(parsed.titles) == 1 and bool(parsed.titles[0]) and not parsed.in_title


def verify_title_change(before, after):
    if len(after.encode()) > MAX_HTML_BYTES or has_title(before) or not has_title(after):
        raise ValueError("The missing-title before/after check failed.")
    matches = list(TITLE.finditer(after))
    head = re.search(r"<head(?:\s[^>]*)?>", after, re.IGNORECASE)
    end = re.search(r"</head\s*>", after, re.IGNORECASE)
    if (
        len(matches) != 1
        or head is None
        or end is None
        or not head.end() <= matches[0].start() < matches[0].end() <= end.start()
        or len(TitleParser(after).titles[0]) > 200
        or TITLE.sub("", before) != TITLE.sub("", after)
    ):
        raise ValueError("The repair must change only one title inside the existing HTML head.")
