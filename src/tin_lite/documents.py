from __future__ import annotations

import math
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlparse

import mistune
from mistune.renderers.html import HTMLRenderer
from mistune.util import escape as escape_text
from mistune.util import escape_url, safe_entity, striptags

WORDS_PER_MINUTE = 220
_WORD = re.compile(r"[\w]+(?:['’\-][\w]+)*", re.UNICODE)
_SLUG_SEPARATOR = re.compile(r"[^\w]+", re.UNICODE)
_SAFE_INLINE_HTML = {"<br>", "<br/>", "<sub>", "</sub>"}


@dataclass(frozen=True)
class MarkdownHeading:
    id: str
    title: str


@dataclass(frozen=True)
class RenderedMarkdown:
    markdown: str
    html: str
    headings: tuple[MarkdownHeading, ...]
    word_count: int
    reading_minutes: int


_CALLOUT = re.compile(r"\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(?:<br />)?\n?")
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(?:svg|html)")
_FIELD = re.compile(r"([a-z]+)[ \t]*:[ \t]*(.+)")
# Mistune's bare-URL pattern, except a URL never ends in a backslash. Backslash escapes inside a
# bare URL are Markdown, not part of the address: older plans escaped every dot and hyphen.
_BARE_URL = r"""https?:\/\/[^\s<]+[^<.,:;"')\]\s\\]"""
_URL_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


class _TinHTMLRenderer(HTMLRenderer):
    def __init__(self, asset_folder: str | None = None) -> None:
        # Raw HTML is handled explicitly below. This lets existing reports keep their harmless
        # <br>/<sub> formatting while every other tag is shown as text instead of executed.
        super().__init__(escape=False)
        self.headings: list[MarkdownHeading] = []
        self._slug_counts: dict[str, int] = {}
        # An article's assets folder (page_assets): figures and embeds the reader loads itself.
        self.asset_folder = asset_folder

    def _asset(self, url: str) -> str | None:
        """The project path of a file in the article's assets folder, or None."""
        if not self.asset_folder:
            return None
        name = self.asset_folder.rsplit("/", 1)[-1]
        relative = url.strip().removeprefix("./")
        if not relative.startswith(f"{name}/"):
            return None
        file = relative[len(name) + 1 :]
        return f"{self.asset_folder}/{file}" if _ASSET_NAME.fullmatch(file) else None

    def heading(self, text: str, level: int, **attrs: object) -> str:
        title = unescape(striptags(text)).strip()
        base = _heading_slug(title)
        count = self._slug_counts.get(base, 0) + 1
        self._slug_counts[base] = count
        heading_id = base if count == 1 else f"{base}-{count}"
        if level == 2:
            self.headings.append(MarkdownHeading(id=heading_id, title=title))
        return f'<h{level} id="{escape_text(heading_id)}">{text}</h{level}>\n'

    def link(self, text: str, url: str, title: str | None = None) -> str:
        rendered = f'<a href="{self.safe_url(url)}"'
        if title:
            rendered += f' title="{safe_entity(title)}"'
        return f'{rendered} rel="noreferrer">{text}</a>'

    def image(self, text: str, url: str, title: str | None = None) -> str:
        alt = striptags(text)
        filename = PurePosixPath(urlparse(url).path).name or "image"
        if asset := self._asset(url):
            # No src: the reader loads the file with the member's session, never by URL.
            rendered = (
                f'<img class="md-asset" data-asset="{safe_entity(asset)}" '
                f'alt="{safe_entity(alt)}" data-fallback-name="{safe_entity(filename)}"'
            )
            if title:
                rendered += f' title="{safe_entity(title)}"'
            return rendered + " />"
        rendered = (
            f'<img src="{self.safe_url(url)}" alt="{safe_entity(alt)}" '
            f'data-fallback-name="{safe_entity(filename)}"'
        )
        if title:
            rendered += f' title="{safe_entity(title)}"'
        return rendered + " />"

    def block_code(self, code: str, info: str | None = None) -> str:
        language = ""
        if info:
            language = info.strip().split(None, 1)[0]
        if language in {"tin-embed", "tin-video"} and self.asset_folder:
            fields = {
                match[1]: match[2].strip()
                for line in code.splitlines()
                if (match := _FIELD.fullmatch(line.strip()))
            }
            title = safe_entity(fields.get("title", ""))
            if language == "tin-embed" and (asset := self._asset(fields.get("src", ""))):
                try:
                    height = min(max(int(fields.get("height", "420")), 120), 1600)
                except ValueError:
                    height = 420
                return (
                    f'<figure class="md-embed" data-asset="{safe_entity(asset)}" '
                    f'data-height="{height}" data-title="{title}"></figure>\n'
                )
            url = fields.get("url", "")
            if language == "tin-video" and url.startswith(("https://", "http://")):
                link = self.safe_url(url)
                poster = fields.get("poster", "")
                poster_attr = (
                    f' data-poster="{self.safe_url(poster)}"'
                    if poster.startswith(("https://", "http://"))
                    else ""
                )
                return (
                    f'<figure class="md-video" data-url="{link}" data-title="{title}"'
                    f'{poster_attr}><a href="{link}" rel="noreferrer">'
                    f"{title or escape_text(url)}</a></figure>\n"
                )
        language_attr = f' data-language="{safe_entity(language)}"' if language else ""
        code_class = f' class="language-{safe_entity(language)}"' if language else ""
        return (
            f'<div class="md-code-block"{language_attr}><pre><code{code_class}>'
            f"{escape_text(code)}</code></pre></div>\n"
        )

    def block_quote(self, text: str) -> str:
        """GitHub's alert syntax (`> [!NOTE]`) as a callout; any other quote as before."""
        match = _CALLOUT.match(text)
        if match is None:
            return super().block_quote(text)
        kind = match[1].lower()
        rest = text[match.end() :].lstrip()
        rest = rest[len("</p>") :] if rest.startswith("</p>") else f"<p>{rest}"
        return (
            f'<aside class="md-callout" data-kind="{kind}">'
            f'<p class="md-callout-label">{kind.title()}</p>{rest}</aside>\n'
        )

    def inline_html(self, html: str) -> str:
        compact = re.sub(r"\s+", "", html).lower()
        if compact in _SAFE_INLINE_HTML:
            return "<br />" if compact in {"<br>", "<br/>"} else compact
        return escape_text(html)

    def block_html(self, html: str) -> str:
        return f"<p>{escape_text(html.strip())}</p>\n"


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _parse_bare_url(inline, match, state) -> int:
    text = _URL_ESCAPE.sub(r"\1", match.group(0))
    if state.in_link:
        inline.process_text(text, state)
    else:
        state.append_token(
            {
                "type": "link",
                "children": [{"type": "text", "raw": text}],
                "attrs": {"url": escape_url(text)},
            }
        )
    return match.end()


def _bare_urls(md) -> None:
    md.inline.register("url_link", _BARE_URL, _parse_bare_url)


def render_markdown(markdown: str, *, asset_folder: str | None = None) -> RenderedMarkdown:
    """Safe HTML for the reader. With an article's `asset_folder`, its figures, embeds and
    videos render as placeholders the reader fills in; otherwise as ordinary Markdown."""
    renderer = _TinHTMLRenderer(asset_folder)
    parser = mistune.create_markdown(
        renderer=renderer,
        plugins=["strikethrough", "table", "task_lists", _bare_urls],
    )
    # Plain scalar frontmatter is source metadata, not article copy. Keep it accessible
    # without a workflow-specific reader or interpreting YAML tags/objects. Unsupported
    # frontmatter stays visible as ordinary Markdown; raw source is always unchanged.
    match = re.match(r"\A---\r?\n([\s\S]{1,16000}?)\r?\n---(?:\r?\n|\Z)", markdown)
    metadata = ""
    body = markdown
    if (
        match
        and all(
            re.fullmatch(r"[A-Za-z_][\w.-]*:[^\r\n]*", line)
            for line in match[1].splitlines()
            if line.strip()
        )
        and match[1].strip()
    ):
        metadata = (
            '<details class="md-document-metadata"><summary>Document metadata</summary>'
            f"<pre><code>{escape_text(match[1])}</code></pre></details>\n"
        )
        body = markdown[match.end() :]
    rendered = parser(body)
    visible = _VisibleText()
    visible.feed(rendered)
    word_count = len(_WORD.findall(" ".join(visible.parts)))
    return RenderedMarkdown(
        markdown=markdown,
        html=rendered + metadata,
        headings=tuple(renderer.headings),
        word_count=word_count,
        reading_minutes=max(1, math.ceil(word_count / WORDS_PER_MINUTE)),
    )


def _heading_slug(title: str) -> str:
    slug = _SLUG_SEPARATOR.sub("-", title.casefold()).strip("-_")
    return slug or "section"
