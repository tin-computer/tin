"""Bounded reads of selected, server-rendered LinkedIn pages. No site scripts execute."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import cached_property
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import tinycss2

from tin_lite.connection_collection import CollectionError, CollectionSource, Person, profile_url


def clean(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


class Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children = []
        self.hidden = (
            bool(parent and parent.hidden)
            or tag in {"script", "style", "template", "noscript", "code"}
            or "hidden" in self.attrs
            or bool(
                re.search(
                    r"display\s*:\s*none|visibility\s*:\s*hidden", self.attrs.get("style", "")
                )
            )
            or bool(set(self.attrs.get("class", "").split()) & {"visually-hidden", "sr-only"})
        )

    @cached_property
    def text(self):
        if self.hidden:
            return ""
        return clean(" ".join(c if isinstance(c, str) else c.text for c in self.children))

    @property
    def raw_text(self):
        if self.tag in {"script", "style", "template", "noscript", "code"}:
            return ""
        return clean(" ".join(c if isinstance(c, str) else c.raw_text for c in self.children))

    def nodes(self, *, include_hidden=False):
        if include_hidden or not self.hidden:
            yield self
            for child in self.children:
                if isinstance(child, Node):
                    yield from child.nodes(include_hidden=include_hidden)

    def ancestors(self):
        node = self.parent
        while node:
            yield node
            node = node.parent

    def require_known_visibility(self):
        if any(n.uncertain_visibility for n in [self, *self.ancestors()]):
            raise CollectionError("unsupported_search_contract")

    @property
    def excluded(self):
        return any(
            n.tag in {"aside", "nav", "footer"}
            or n.attrs.get("role") in {"navigation", "complementary", "dialog"}
            for n in [self, *self.ancestors()]
        )


class Document(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, html, styles=()):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack, self.count = [self.root], 0
        self.feed(html)
        self.close()
        if not re.search(r"</html\s*>\s*$", html, re.I):
            raise CollectionError("unsupported_search_contract")
        self.apply_styles(styles)

    def apply_styles(self, styles):
        # Only unconditional atomic visibility declarations are supported. Class names
        # come from the page's own stylesheet, never a pinned provider-generated name.
        rules, conditional = [], set()

        def collect(items, depth=0, uncertain=False):
            if depth > 8:
                raise CollectionError("unsupported_search_contract")
            for item in items:
                if item.type == "at-rule" and item.content:
                    collect(
                        tinycss2.parse_blocks_contents(item.content),
                        depth + 1,
                        uncertain or item.lower_at_keyword != "layer",
                    )
                elif item.type == "qualified-rule":
                    selector = tinycss2.serialize(item.prelude).strip()
                    match = re.fullmatch(r"\.([A-Za-z_][A-Za-z0-9_-]*)", selector)
                    if not match:
                        continue
                    for declaration in tinycss2.parse_blocks_contents(item.content):
                        if declaration.type == "declaration" and declaration.lower_name in {
                            "display",
                            "visibility",
                            "opacity",
                        }:
                            if uncertain:
                                conditional.add(match[1])
                                continue
                            rules.append(
                                (
                                    match[1],
                                    declaration.lower_name,
                                    tinycss2.serialize(declaration.value).strip(),
                                )
                            )

        for css in styles:
            collect(tinycss2.parse_stylesheet(css))
        by_class = {}
        for class_name, prop, value in rules:
            by_class.setdefault(class_name, {}).setdefault(prop, set()).add(value)
        for node in self.root.nodes(include_hidden=True):
            declarations = {}
            classes = node.attrs.get("class", "").split()
            for class_name in classes:
                for prop, values in by_class.get(class_name, {}).items():
                    declarations.setdefault(prop, set()).update(values)
            node.uncertain_visibility = bool(conditional.intersection(classes)) or any(
                len(values) > 1 for values in declarations.values()
            )
            # Never guess which responsive or conflicting visibility rule applies.
            hidden = declarations.get("display") == {"none"}
            node.visibility_hidden = declarations.get("visibility") == {"hidden"} or bool(
                re.search(r"visibility\s*:\s*hidden", node.attrs.get("style", ""))
            )
            hidden |= node.visibility_hidden or declarations.get("opacity") == {"0"}
            node.hidden = (
                node.hidden
                or (hidden and not node.uncertain_visibility)
                or bool(node.parent and node.parent.hidden)
            )
            node.__dict__.pop("text", None)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 50_000 or len(self.stack) > 100:
            raise CollectionError("unsupported_search_contract")
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not any(
            n.tag in {"script", "style", "template", "noscript", "code"} for n in self.stack
        ):
            self.stack[-1].children.append(data)

    def content(self):
        mains = [n for n in self.root.nodes() if n.tag == "main" or n.attrs.get("role") == "main"]
        outer = [n for n in mains if not any(a in mains for a in n.ancestors())]
        if len(outer) > 1:
            raise CollectionError("unsupported_search_contract")
        return outer[0] if outer else self.root


def relationship(node):
    found = []
    for item in node.nodes():
        if item.excluded:
            continue
        for child in item.children:
            if isinstance(child, str):
                match = re.fullmatch(r"[·•]?\s*([123](?:st|nd|rd))\+?", clean(child))
                if match:
                    item.require_known_visibility()
                    found.append(match[1])
    return found


def link_profile(node, *, allow_navigation=False):
    if node.tag != "a" or (node.excluded and not allow_navigation):
        return None
    try:
        url = profile_url(urljoin("https://www.linkedin.com", node.attrs.get("href", "")))
    except (CollectionError, ValueError):
        return None
    node.require_known_visibility()
    return url


def verify_document(document):
    """Check page restrictions; read_page binds the same cookies to /me before each read."""
    nodes = list(document.root.nodes())
    if any(
        n.tag == "input"
        and n.attrs.get("name") == "session_password"
        or n.tag == "iframe"
        and "captcha" in n.attrs.get("src", "")
        for n in nodes
    ):
        raise CollectionError("challenge")
    notices = " ".join(
        n.text for n in nodes if n.attrs.get("role") == "alert" or n.tag in {"h1", "h2"}
    )
    if re.search(
        r"commercial use limit|search limit|unusual activity|"
        r"temporarily restricted|too many requests",
        notices,
        re.I,
    ):
        raise CollectionError("rate_limited")
    html_node = next((n for n in nodes if n.tag == "html"), None)
    if (
        html_node
        and html_node.attrs.get("lang")
        and not re.match(r"en(?:-|$)", html_node.attrs["lang"], re.I)
    ):
        raise CollectionError("unsupported_search_contract")


def prepare_source(document, source, profile, keywords):
    expected = profile_url(source["friend_url"])
    if profile_url("https://www.linkedin.com/in/" + profile["publicIdentifier"]) != expected:
        raise CollectionError("filters_changed")
    urn = profile.get("entityUrn", "")
    if not re.fullmatch(r"urn:li:fsd_profile:[A-Za-z0-9_-]{1,256}", urn):
        raise CollectionError("browser_preparation_required")
    member = urn.rsplit(":", 1)[-1]
    name = clean(" ".join(profile.get(k, "") for k in ("firstName", "lastName")))
    if not name:
        raise CollectionError("browser_preparation_required")
    content = document.content()
    matched = False
    for node in content.nodes():
        if (
            node.tag != "a"
            or node.excluded
            or "connections" not in node.text.lower()
            or re.search(r"mutual|shared", node.text, re.I)
        ):
            continue
        url = urlsplit(urljoin("https://www.linkedin.com", node.attrs.get("href", "")))
        if url.netloc != "www.linkedin.com" or url.path.rstrip("/") != "/search/results/people":
            continue
        try:
            targets = json.loads(parse_qs(url.query).get("connectionOf", [""])[0])
        except ValueError:
            continue
        if targets != [member]:
            continue
        for scope in node.ancestors():
            if scope is content or scope.tag in {"body", "document"}:
                break
            headings = [n for n in scope.nodes() if n.tag in {"h1", "h2"} and n.text == name]
            if not headings:
                continue
            if len(headings) != 1:
                break
            # Social-proof cards can have other relationship badges inside the top card.
            # Bind the badge to the selected person's heading, not the whole section.
            for heading_scope in headings[0].ancestors():
                degrees = relationship(heading_scope)
                if degrees:
                    if len(degrees) == 1:
                        if degrees != ["1st"]:
                            raise CollectionError("friend_not_connected")
                        matched = True
                    break
                if heading_scope is scope:
                    break
            break
    if not matched:
        raise CollectionError("browser_preparation_required")
    result = CollectionSource(
        friend_url=expected,
        friend_name=name[:200],
        actor=source["actor"],
        first_degree=True,
        collection_url="https://www.linkedin.com/search/results/people/?"
        + urlencode(
            {"connectionOf": json.dumps([member]), "network": '["S"]', "keywords": keywords}
        ),
    )
    result.scope(keywords=keywords)
    return result.model_dump()


def search_results(document, page):
    content = document.content()
    people = {}
    for link in content.nodes():
        url = link_profile(link)
        if not url or not link.text:
            continue
        candidate = None
        for scope in link.ancestors():
            if scope is content or scope.tag in {"body", "document"}:
                break
            urls = {u for n in scope.nodes() if (u := link_profile(n))}
            if len(urls) != 1:
                break
            degrees = relationship(scope)
            if len(degrees) > 1:
                break
            if not degrees:
                continue
            if degrees != ["2nd"]:
                raise CollectionError("filters_changed")
            name = link.text
            visible_names = [
                n.text for n in link.nodes() if n.attrs.get("aria-hidden") == "true" and n.text
            ]
            if len(visible_names) == 1:
                name = visible_names[0]
            candidate = Person(
                profile_url=url, name=name[:200], degree="2nd", visible_text=scope.text[:3000]
            ).model_dump()
            if scope.tag in {"li", "article"} or scope.attrs.get("role") == "listitem":
                break
        if candidate:
            people.setdefault(url, candidate)
    if len(people) > 10:
        raise CollectionError("unsupported_search_contract")
    empty = any(
        re.match(
            r"^(no results found|no results for|no matching results)(?:[.!]|$|\s)", n.text, re.I
        )
        for n in content.nodes()
        if n.tag in {"h1", "h2", "h3", "p"} and not n.excluded
    )
    if empty:
        if page != 1 or people or any(link_profile(n) for n in content.nodes()):
            raise CollectionError("unsupported_search_contract")
        return {"people": [], "next_page": False}
    controls = [
        n
        for n in content.nodes()
        if not n.excluded and (n.tag in {"button", "a"} or n.attrs.get("role") == "button")
    ]
    current = [
        n
        for n in content.nodes()
        if not n.excluded and n.attrs.get("aria-current") in {"page", "true"}
    ]

    def number(node):
        return re.findall(r"\d+", node.attrs.get("aria-label") or node.text)

    if len(current) != 1 or number(current[0]) != [str(page)]:
        raise CollectionError("unsupported_search_contract")
    current[0].require_known_visibility()
    next_buttons = [
        n
        for n in controls
        if re.fullmatch(r"next(?: page)?", n.attrs.get("aria-label") or n.text, re.I)
    ]
    if len(next_buttons) == 1:
        next_button = next_buttons[0]
        next_button.require_known_visibility()
        more = (
            "disabled" not in next_button.attrs and next_button.attrs.get("aria-disabled") != "true"
        )
    elif not next_buttons:
        hidden_next = [
            n
            for n in content.nodes(include_hidden=True)
            if n.tag in {"button", "a"}
            and not n.excluded
            and n.visibility_hidden
            and re.fullmatch(r"next(?: page)?", n.attrs.get("aria-label") or n.raw_text, re.I)
        ]
        if len(hidden_next) != 1 or not final_pager(hidden_next[0], current[0], page):
            raise CollectionError("unsupported_search_contract")
        more = False
    else:
        raise CollectionError("unsupported_search_contract")
    if not people:
        raise CollectionError("unsupported_search_contract")
    return {"people": list(people.values()), "next_page": more}


def final_pager(next_button, current, page):
    """A hidden Next alone cannot establish completion; require the numbered final pager."""
    if next_button.parent.hidden:
        return False
    for scope in next_button.ancestors():
        if scope.tag in {"main", "body", "document"} or any(link_profile(n) for n in scope.nodes()):
            return False
        if current not in scope.nodes():
            continue
        if re.search(r"…|\.{3}", scope.text):
            return False
        controls = [n for n in scope.nodes() if n.tag in {"button", "a"}]
        previous = [
            n
            for n in controls
            if re.fullmatch(r"previous(?: page)?", n.attrs.get("aria-label") or n.text, re.I)
        ]
        if len(previous) != 1:
            return False
        disabled = (
            "disabled" in previous[0].attrs or previous[0].attrs.get("aria-disabled") == "true"
        )
        if disabled != (page == 1):
            return False
        numbers = []
        for control in controls:
            if control is previous[0]:
                continue
            match = re.fullmatch(
                r"(?:page\s+)?([1-9]\d*)", control.attrs.get("aria-label") or control.text, re.I
            )
            if not match:
                return False
            numbers.append(int(match[1]))
        return (
            bool(numbers)
            and numbers[-1] == page
            and all(value == numbers[0] + index for index, value in enumerate(numbers))
        )
    return False


async def get_document(client, session, url):
    from tin_lite.linkedin_session import headers

    request_headers = headers(session)
    request_headers.update(
        {
            "accept": "text/html,application/xhtml+xml",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "referer": "https://www.linkedin.com/feed/",
        }
    )
    async with client.stream(
        "GET", url, headers=request_headers, follow_redirects=False
    ) as response:
        if response.status_code != 200:
            code = {
                401: "session_expired",
                403: "access_denied",
                429: "rate_limited",
                999: "access_denied",
            }.get(response.status_code, "cloud_failed")
            if 300 <= response.status_code < 400:
                code = (
                    "challenge"
                    if re.search(r"checkpoint|challenge", response.headers.get("location", ""))
                    else "session_expired"
                )
            raise CollectionError(code)
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 2_000_000:
                raise CollectionError("unsupported_search_contract")
    document = Document(data.decode("utf-8", errors="replace"))
    stylesheets = list(
        dict.fromkeys(
            n.attrs.get("href", "")
            for n in document.root.nodes()
            if n.tag == "link" and n.attrs.get("rel") == "stylesheet"
        )
    )
    if len(stylesheets) > 3:
        raise CollectionError("unsupported_search_contract")
    styles = []
    for stylesheet in stylesheets:
        if not re.fullmatch(
            r"https://static\.licdn\.com/aero-v1/sc/h/assets/[A-Za-z0-9_-]+\.css", stylesheet
        ):
            raise CollectionError("unsupported_search_contract")
        # A fresh request has no inherited headers or cookie jar. Static assets never
        # receive the account credential, even when a caller supplied a custom client.
        import httpx

        request = httpx.Request("GET", stylesheet, headers={"accept": "text/css"})
        response = await client.send(request, stream=True, follow_redirects=False, auth=None)
        try:
            if response.status_code != 200:
                raise CollectionError("unsupported_search_contract")
            css = bytearray()
            async for chunk in response.aiter_bytes():
                css.extend(chunk)
                if len(css) > 2_000_000:
                    raise CollectionError("unsupported_search_contract")
            styles.append(css.decode("utf-8", errors="replace"))
        finally:
            await response.aclose()
    document.apply_styles(styles)
    return document


async def read_document_page(client, session, source, keywords, page):
    from tin_lite.linkedin_session import _get

    resolved = None
    if "collection_url" not in source:
        friend = profile_url(source["friend_url"])
        identifier = unquote(friend.rsplit("/", 1)[1])
        payload = await _get(
            client,
            session,
            "/voyager/api/identity/dash/profiles",
            {"q": "memberIdentity", "memberIdentity": identifier},
        )
        stack, profiles, visited = [payload], {}, 0
        while stack:
            value = stack.pop()
            visited += 1
            if visited > 5000:
                raise CollectionError("browser_preparation_required")
            if isinstance(value, dict):
                if value.get("publicIdentifier") == identifier and value.get("entityUrn"):
                    profiles[value["entityUrn"]] = value
                stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
            elif isinstance(value, list):
                stack.extend(value)
        if len(profiles) != 1:
            raise CollectionError("browser_preparation_required")
        document = await get_document(client, session, friend)
        verify_document(document)
        resolved = prepare_source(document, source, next(iter(profiles.values())), keywords)
        source = resolved
    selected = CollectionSource.model_validate(source)
    selected.scope(keywords=keywords)
    from tin_lite.connection_collection import view_url

    document = await get_document(client, session, view_url(selected.collection_url, page))
    verify_document(document)
    result = search_results(document, page)
    if resolved:
        result["source"] = resolved
    return result
