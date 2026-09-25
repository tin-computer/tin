"""Bounded reader for a founder's public site: the homepage, one page per type the plan rules name,
llms.txt and the sitemap. The URL comes from a run input, so every hop is resolved first and refused
unless all its addresses are public; the connection then goes to the vetted address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 Tin-Growth-Plan/1.0"
)
KINDS = {
    "pricing": r"pric|plans",
    "docs": r"docs|documentation|help|guide",
    "about": r"about|company|team",
    "blog": r"blog|news|articles|resources",
    "changelog": r"changelog|releases|whats-new|updates",
    "faq": r"faq|questions",
    "product": r"features|product|how-it-works|use-cases|customers|services|solutions",
}
CHALLENGE = re.compile(
    r"(?i)just a moment|cf-chl|captcha|access denied|px-captcha|datadome|attention required"
)
PAGE_CHARS = 4500
MAX_BODY_BYTES = 3_000_000
MAX_REDIRECTS = 5
REQUEST_SECONDS = 12
TOTAL_SECONDS = 40


def _meta(html, name):
    m = re.search(
        rf'(?is)<meta[^>]+(?:name|property)=["\']{name}["\'][^>]+content=["\'](.*?)["\']', html
    )
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def extract(html):
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    body = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?is)<(h[1-4]|p|li|br|div|section|tr)[^>]*>", "\n", body)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    text = (
        re.sub(r"&nbsp;|&#160;", " ", text)
        .replace("&amp;", "&")
        .replace("&#x27;", "'")
        .replace("&quot;", '"')
    )
    lines, seen = [], set()
    for line in (re.sub(r"[ \t]+", " ", x).strip() for x in text.split("\n")):
        if len(line) > 2 and line not in seen:
            seen.add(line)
            lines.append(line)
    return {
        "title": re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
        "description": _meta(html, "description") or _meta(html, "og:description"),
        "text": "\n".join(lines),
    }


def links(html, base):
    host = urlparse(base).netloc.removeprefix("www.")
    out = {}
    for href, label in re.findall(r'(?is)<a[^>]+href=["\'](.*?)["\'][^>]*>(.*?)</a>', html):
        url = urljoin(base, href.split("#")[0])
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.removeprefix("www.") != host:
            continue
        path = parsed.path.rstrip("/").lower()
        if not path or path.count("/") > 2:
            continue
        label = re.sub(r"(?s)<[^>]+>", " ", label).lower()
        for kind, pattern in KINDS.items():
            if kind not in out and (
                re.search(pattern, path.rsplit("/", 1)[-1]) or re.search(rf"\b({pattern})\b", label)
            ):
                out[kind] = url
    return out


def _same_site(a, b):
    return (a or "").removeprefix("www.") == (b or "").removeprefix("www.")


async def _public_ip(host, port, resolver):
    addresses = await resolver(host, port, type=socket.SOCK_STREAM)
    # Keep the resolver's route preference; lexical sorting can select unreachable IPv6.
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("non_public_address")
    return ips[0]


async def _fetch(client, url, resolver):
    """One GET with manual, same-site redirects; every hop is vetted before it is contacted."""
    site_host = urlparse(url).hostname
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("unsupported_url")
        if not _same_site(parsed.hostname, site_host):
            raise ValueError("outside_site_redirect")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip = await _public_ip(parsed.hostname, port, resolver)
        request = client.build_request(
            "GET",
            httpx.URL(url).copy_with(host=ip),
            headers={"Host": parsed.netloc, "User-Agent": USER_AGENT},
            extensions={"sni_hostname": parsed.hostname},
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    break
            encoding = response.charset_encoding or "utf-8"
            return (
                url,
                response.status_code,
                response.headers.get("content-type", ""),
                bytes(body[:MAX_BODY_BYTES]).decode(encoding, "replace"),
            )
        finally:
            await response.aclose()
    raise ValueError("redirect_limit")


async def _get(client, url, resolver):
    started = time.monotonic()
    try:
        final_url, status, content_type, text = await _fetch(client, url, resolver)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError, LookupError) as exc:
        # Never carry network exceptions or headers into the plan; the kind is enough.
        kind = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return {
            "url": url,
            "status": None,
            "error": kind,
            "seconds": round(time.monotonic() - started, 1),
        }
    html = text if "html" in (content_type or "html") or url.endswith(".txt") else ""
    page = {
        "url": final_url,
        "status": status,
        "seconds": round(time.monotonic() - started, 1),
        "html": html,
    }
    page.update(
        {"title": "", "description": "", "text": html} if url.endswith(".txt") else extract(html)
    )
    page["challenge"] = bool(status in (401, 403, 429, 503) or CHALLENGE.search(html[:8000]))
    return page


async def sitemap_summary(client, base, resolver):
    """URL count and largest path groups: page depth shows even when pages are JS shells."""
    try:
        _, status, _, text = await _fetch(client, urljoin(base, "/sitemap.xml"), resolver)
        if status != 200 or "<loc>" not in text:
            return None
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text)
        if locs and all(
            x.endswith(".xml") for x in locs[:5]
        ):  # a sitemap index: up to three children
            children = await asyncio.gather(
                *(_fetch(client, x, resolver) for x in locs[:3]), return_exceptions=True
            )
            locs = [
                y
                for child in children
                if not isinstance(child, BaseException)
                for y in re.findall(r"<loc>\s*(.*?)\s*</loc>", child[3])
            ]
        groups = {}
        for x in locs:
            first = urlparse(x).path.strip("/").split("/")[0] or "(home)"
            groups[first] = groups.get(first, 0) + 1
        top = sorted(groups.items(), key=lambda kv: -kv[1])[:8]
        return {
            "url": urljoin(base, "/sitemap.xml"),
            "urls": len(locs),
            "top_sections": top,
            "sample": locs[:12],
        }
    except (httpx.HTTPError, OSError, TimeoutError, ValueError, LookupError):
        return None


async def read_site(url, *, client=None, resolver=None, html_scan=None):
    """Pages plus a verdict: ok | thin (a JavaScript shell) | blocked | unreachable | none.

    `html_scan(html) -> dict` lets a caller keep bounded signals from the raw page (tracking
    tags, for example) before the HTML is dropped; the default keeps none."""
    if not url:
        return {"verdict": "none", "pages": [], "seconds": 0}
    started = time.monotonic()
    own_client = client is None
    client = client or httpx.AsyncClient(
        trust_env=False,
        timeout=REQUEST_SECONDS,
        follow_redirects=False,
        headers={"Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"},
    )
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            home = await _get(client, url, resolver)
            if home.get("status") is None:
                home.update(kind="home", challenge=False, chars=0, text="")
                return {
                    "verdict": "unreachable",
                    "pages": [home],
                    "seconds": round(time.monotonic() - started, 1),
                }
            found = links(home.get("html", ""), home["url"])
            base = home["url"]
            targets = dict(found)
            for kind, path in (
                ("pricing", "/pricing"),
                ("about", "/about"),
                ("blog", "/blog"),
                ("faq", "/faq"),
                ("docs", "/docs"),
            ):
                targets.setdefault(
                    kind, urljoin(base, path)
                )  # one guess per page type, as the rules allow
            targets["llms"] = urljoin(base, "/llms.txt")
            rest = await asyncio.gather(*(_get(client, u, resolver) for u in targets.values()))
            sitemap = await sitemap_summary(client, base, resolver)
    except TimeoutError:
        return {
            "verdict": "unreachable",
            "pages": [
                {
                    "url": url,
                    "status": None,
                    "error": "timeout",
                    "kind": "home",
                    "challenge": False,
                    "chars": 0,
                    "text": "",
                }
            ],
            "seconds": round(time.monotonic() - started, 1),
        }
    finally:
        if own_client:
            await client.aclose()
    pages = [dict(home, kind="home")]
    for kind, page in zip(targets, rest, strict=True):
        page["kind"] = kind
        page["linked"] = kind in found
        pages.append(page)
    for page in pages:
        if html_scan is not None:
            page["signals"] = html_scan(page.get("html") or "")
        page.pop("html", None)
        page["chars"] = len(page.get("text", ""))
        page["text"] = page.get("text", "")[:PAGE_CHARS]
        page.setdefault("challenge", False)
    good = [
        p
        for p in pages
        if p.get("status") == 200
        and not p["challenge"]
        and p["chars"] > 400
        and not (p["kind"] != "home" and p["url"].rstrip("/") == pages[0]["url"].rstrip("/"))
    ]
    if pages[0]["challenge"]:
        verdict = "blocked"
    elif sum(p["chars"] for p in good) < 1500:
        verdict = "thin"
    else:
        verdict = "ok"
    return {
        "verdict": verdict,
        "sitemap": sitemap,
        "pages": pages,
        "readable": [p["url"] for p in good],
        "seconds": round(time.monotonic() - started, 1),
    }


def evidence_text(site):
    if site["verdict"] == "none":
        return "No product_url: there is no public site."
    parts = [f"Direct fetch verdict: {site['verdict']}."]
    if site.get("sitemap"):
        m = site["sitemap"]
        parts.append(
            f"[sitemap] {m['url']} lists {m['urls']} URLs. "
            f"Largest sections: {m['top_sections']}. Sample: {m['sample']}. "
            "This establishes pages, not indexing or traffic."
        )
    for p in site["pages"]:
        if p.get("status") != 200 or p["challenge"] or p["chars"] < 200:
            if p["kind"] != "llms":
                outcome = (
                    "challenge/blocked" if p.get("challenge") else p.get("status") or p.get("error")
                )
                parts.append(f"[{p['kind']}] {p['url']} -> {outcome}: none found after one fetch")
            continue
        parts.append(
            f"[{p['kind']}] {p['url']}\nTITLE: {p['title']}\n"
            f"DESCRIPTION: {p['description']}\n{p['text']}"
        )
    return "\n\n".join(parts)
