"""Feed and page parsing shared by every intel fetcher, world and client alike.

RSS 2.0, Atom and RDF feeds; article pages read for their date, title, author and first
paragraphs; and the small helpers every fetcher uses: `strip_html`, `clean_url` (one list of
tracking parameters) and `parse_date`. Ported from the prototype's rss.py, which the world
fetchers now share too. Everything parsed is untrusted data: it is read, never executed or
followed.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET  # noqa: S405 — no DTDs or entities expand in ElementTree
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from tin_lite.intel_fetch.http import PublicReader

ENRICH_CHARS = 1800
# Tracking parameters every fetcher drops from an item's URL: one list for the world feed,
# the client's feeds and pages, and HN and Reddit links, so one article cleans to one URL.
TRACKING = re.compile(
    r"^(utm_.*|ref|ref_src|ref_url|reflink|oc|cmpid|taid|ncid|smid|smtyp|sr_share|st|mod|"
    r"mc_cid|mc_eid|fbclid|gclid|igshid|si|s|guccounter|guce_referrer.*|at_.*|itm_.*|"
    r"_fb_noscript|_hsenc|_hsmi|mkt_tok|source|src|rss|feed|campaign)$",
    re.I,
)
MONTH_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r" \d{1,2}, 20\d\d"
)


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except (TypeError, ValueError, IndexError):
        pass
    t = s.replace("Z", "+00:00")
    t = re.sub(r"(\.\d{6})\d+", r"\1", t)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        t += "T00:00:00+00:00"
    try:
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except ValueError:
        pass
    # Zone-less US formats (e.g. "Nov 23, 2026 3:17pm"): assume US Eastern standard time.
    for fmt in ("%b %d, %Y %I:%M%p", "%B %d, %Y %I:%M%p", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return (datetime.strptime(s, fmt) + timedelta(hours=5)).replace(tzinfo=UTC)  # noqa: DTZ007
        except ValueError:
            pass
    return None


def clean_url(u: str | None) -> str | None:
    """The URL without tracking parameters or a fragment, its host lowercased."""
    if not u:
        return u
    u = html.unescape(u.strip())
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    q = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if not TRACKING.match(k)
    ]
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc.lower(), p.path, urllib.parse.urlencode(q), "")
    )


def ln(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_xml(body: bytes) -> ET.Element:
    body = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", body)
    # A feed never needs a DTD: entity declarations are refused (no expansion of any kind),
    # and a bare DOCTYPE line is dropped.
    if re.search(rb"<!ENTITY", body, re.I):
        raise ValueError("the document declares XML entities")
    body = re.sub(rb"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", b"", body, count=1, flags=re.I)
    return ET.fromstring(body)  # noqa: S314 — no DTD or entity declarations reach the parser


def parse_feed(body: bytes) -> list[dict]:
    root = parse_xml(body)
    if ln(root.tag) not in ("rss", "feed", "rdf"):
        raise ValueError(f"not a feed (root <{ln(root.tag)}>)")
    out = []
    for el in root.iter():
        if ln(el.tag) not in ("item", "entry"):
            continue
        it = {
            "title": None,
            "link": None,
            "author": None,
            "date": None,
            "text": "",
            "source_name": None,
            "source_site": None,
        }
        desc = content = ""
        updated = None
        for c in el:
            t = ln(c.tag)
            txt = c.text or ""
            if t == "title":
                it["title"] = strip_html("".join(c.itertext())) or None
            elif t == "link":
                href, rel = c.get("href"), c.get("rel", "alternate")
                if href and rel == "alternate" and not it["link"]:
                    it["link"] = href.strip()
                elif not href and txt.strip():
                    it["link"] = txt.strip()
            elif t in ("pubdate", "published", "date", "issued") and not it["date"]:
                it["date"] = txt.strip()
            elif t == "updated":
                updated = txt.strip()
            elif t in ("creator", "author") and not it["author"]:
                nm = None
                for cc in c:
                    if ln(cc.tag) == "name":
                        nm = cc.text
                a = (nm or "".join(c.itertext())).strip()
                it["author"] = a or None
            elif t in ("description", "summary"):
                desc = "".join(c.itertext())
            elif t in ("encoded", "content"):
                content = "".join(c.itertext())
            elif t == "source":
                it["source_name"] = (txt or "").strip() or None
                it["source_site"] = c.get("url")
            elif (
                t == "guid"
                and not it["link"]
                and c.get("isPermaLink", "true") != "false"
                and txt.startswith("http")
            ):
                it["link"] = txt.strip()
        if not it["date"]:
            it["date"] = updated
        a, b = strip_html(content), strip_html(desc)
        it["text"] = a if len(a) > len(b) else b
        out.append(it)
    return out


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.times: list[str] = []
        self.jsonld: list[str] = []
        self.paras: list[str] = []
        self.title = ""
        self._skip = 0
        self._in: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in (
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
            "header",
            "form",
            "aside",
        ):
            if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
                self._in = "ld"
                self._buf = []
            else:
                self._skip += 1
            return
        if tag == "meta":
            k = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if k and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"]
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag == "title":
            self._in = "title"
            self._buf = []
        elif tag == "p" and not self._skip:
            self._in = "p"
            self._buf = []

    def handle_endtag(self, tag):
        if (
            tag in ("style", "noscript", "svg", "nav", "footer", "header", "form", "aside")
            and self._skip
        ):
            self._skip -= 1
        elif tag == "script":
            if self._in == "ld":
                self.jsonld.append("".join(self._buf))
                self._in = None
            elif self._skip:
                self._skip -= 1
        elif tag == "title" and self._in == "title":
            self.title = "".join(self._buf).strip()
            self._in = None
        elif tag == "p" and self._in == "p":
            t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(t) >= 60:
                self.paras.append(t)
            self._in = None

    def handle_data(self, data):
        if self._in in ("ld", "title") or (self._in == "p" and not self._skip):
            self._buf.append(data)


def _ld_values(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            else:
                yield from _ld_values(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _ld_values(v, key)


def page_facts(body: bytes, final_url: str, text_date: bool = False) -> dict:
    """An article page's date, title, author, description and first paragraphs."""
    p = PageParser()
    p.feed(body.decode("utf-8", "replace"))
    m = p.meta
    date = None
    for k in (
        "article:published_time",
        "og:article:published_time",
        "datepublished",
        "parsely-pub-date",
        "sailthru.date",
        "publish-date",
        "pubdate",
        "date",
        "dc.date",
        "citation_publication_date",
    ):
        if m.get(k):
            date = parse_date(m[k])
            if date:
                break
    author = m.get("author") or m.get("article:author") or m.get("parsely-author")
    for raw in p.jsonld:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not date:
            for v in _ld_values(obj, "datePublished"):
                if isinstance(v, str) and parse_date(v):
                    date = parse_date(v)
                    break
        if not author:
            for v in _ld_values(obj, "author"):
                v = v[0] if isinstance(v, list) and v else v
                if isinstance(v, dict) and isinstance(v.get("name"), str):
                    author = v["name"]
                    break
                if isinstance(v, str):
                    author = v
                    break
    if not date and p.times:
        date = parse_date(p.times[0])
    if not date and text_date:
        # Opt-in per source: the first "Month D, YYYY" in the raw page, midnight US Eastern.
        mt = MONTH_DATE.search(body.decode("utf-8", "replace"))
        date = parse_date(mt.group(0)) if mt else None
    title = m.get("og:title") or m.get("twitter:title") or p.title or None
    desc = m.get("og:description") or m.get("description") or ""
    body_text = ""
    for t in p.paras:
        if len(body_text) > ENRICH_CHARS:
            break
        body_text += t + "\n"
    return {
        "url": final_url,
        "date": date,
        "title": html.unescape(title).strip() if title else None,
        "author": author if isinstance(author, str) and not author.startswith("http") else None,
        "description": html.unescape(desc).strip(),
        "paragraphs": body_text.strip(),
    }


async def read_page(
    reader: PublicReader, url: str, text_date: bool = False
) -> tuple[dict | None, str | None]:
    response = await reader.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    if response.status != 200 or not response.body:
        return None, response.error or f"HTTP {response.status}"
    try:
        return page_facts(response.body, response.url, text_date), None
    except Exception as exc:  # noqa: BLE001 — a page that won't parse is a skipped page
        return None, f"html parse: {exc}"[:200]
