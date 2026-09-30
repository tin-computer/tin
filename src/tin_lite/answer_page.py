from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

MAX_ANSWER_PAGE_SOURCE_BYTES = 300_000
MAX_ANSWER_PAGE_BYTES = 150_000
MAX_ANSWER_PAGE_EVIDENCE_BYTES = 1_000_000
MAX_WEB_SOURCES = 30
# A pinned suite carrying this marker asks for the search-structured page; older pins keep
# their four searches and the original page checks.
SEARCH_STRUCTURE_MARKER = "ANSWER_SEO_V1"
SEARCH_STRUCTURE = "answer-seo-v1"
# A pinned suite carrying this marker gets one repair call, without web search, when the
# paid draft misses a page check. Older pins fail the run as before.
REPAIR_MARKER = "ANSWER_REPAIR_V1"
# A pinned suite carrying this marker also receives the project's brand guide and founder
# notes as sources, so the page presents the product the way the project does.
POSITIONING_MARKER = "ANSWER_POSITIONING_V1"
MAX_SEARCH_CALLS = 4
MAX_STRUCTURED_SEARCH_CALLS = 12
MAX_META_TITLE = 60
META_DESCRIPTION_RANGE = (70, 160)
ANSWER_WORDS_RANGE = (25, 90)  # The skill asks for 40-60 words; this catches a missing answer.
MIN_CITED_SOURCES = 3
MIN_INLINE_CITATIONS = 2
MAX_PARAGRAPH_WORDS = 150


class ResponsesClient(Protocol):
    model: str

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class AnswerPageProtocolError(RuntimeError):
    """The model returned data outside the answer-page contract."""


@dataclass(frozen=True)
class AnswerPageSource:
    label: str
    artifact_ref: str
    content: str


class AnswerPageDrafter:
    def __init__(self, *, responses: ResponsesClient, skill_suite: str) -> None:
        self._responses = responses
        self._skill_suite = skill_suite

    @property
    def reads_positioning(self) -> bool:
        return POSITIONING_MARKER in self._skill_suite

    async def draft(
        self,
        *,
        project_name: str,
        sources: list[AnswerPageSource],
        today: str | None = None,
    ) -> dict[str, Any]:
        source_bytes = sum(len(source.content.encode()) for source in sources)
        if source_bytes > MAX_ANSWER_PAGE_SOURCE_BYTES:
            raise ValueError(f"answer-page sources exceed {MAX_ANSWER_PAGE_SOURCE_BYTES} bytes")
        structured = SEARCH_STRUCTURE_MARKER in self._skill_suite
        reference: dict[str, Any] = {
            "project_name": project_name,
            "sources": [
                {
                    "label": source.label,
                    "artifact_ref": source.artifact_ref,
                    "content": source.content,
                }
                for source in sources
            ],
        }
        if structured:
            reference["today"] = today or datetime.now(UTC).date().isoformat()
        response = await self._responses.create(
            {
                "instructions": self._skill_suite,
                "input": json.dumps(reference, separators=(",", ":")),
                "store": False,
                "tools": [{"type": "web_search"}],
                "tool_choice": {"type": "web_search"},
                "include": ["web_search_call.action.sources"],
                "max_tool_calls": MAX_STRUCTURED_SEARCH_CALLS if structured else MAX_SEARCH_CALLS,
                "text": {"verbosity": "medium"},
            }
        )
        result = _normalize_response(response)
        if "ANSWER_PLAN_V1" in self._skill_suite:
            result["argument_plan"], result["markdown"] = extract_argument_plan(result["markdown"])
        if structured:
            result["markdown"] = normalize_search_metadata(
                result["markdown"], today=reference["today"]
            )
            result["structure"] = SEARCH_STRUCTURE
        result["model"] = self._responses.model
        if REPAIR_MARKER in self._skill_suite:
            # The paid research is kept; the activity asks for one repair of these checks.
            problems = answer_page_problems(result["markdown"].encode(), structured=structured)
            if problems:
                result["failed_checks"] = problems
            return result
        validate_answer_page(result["markdown"].encode(), structured=structured)
        return result

    async def repair(self, *, draft: dict[str, Any], today: str | None = None) -> dict[str, Any]:
        """Send the draft and its exact failed checks back once, with no web search.

        The research (queries, sources, citations) is the draft's own; the repaired page
        replaces only the Markdown. The result keeps `failed_checks` when checks still fail.
        """
        failed = draft.get("failed_checks")
        if not isinstance(failed, list) or not failed:
            raise ValueError("answer-page repair needs the draft's failed checks")
        structured = draft.get("structure") == SEARCH_STRUCTURE
        reference: dict[str, Any] = {
            "repair": {
                "failed_checks": failed,
                "page": draft["markdown"],
                "research": {
                    "queries": draft.get("queries", []),
                    "sources": draft.get("sources", []),
                    "citations": draft.get("citations", []),
                },
            }
        }
        if structured:
            reference["today"] = today or datetime.now(UTC).date().isoformat()
        response = await self._responses.create(
            {
                "instructions": self._skill_suite,
                "input": json.dumps(reference, separators=(",", ":")),
                "store": False,
                "text": {"verbosity": "medium"},
            }
        )
        repaired = _normalize_response(response, require_search=False)
        markdown = repaired["markdown"]
        if markdown.lstrip().startswith("<!-- tin-answer-plan-v1"):
            # The saved plan from the research call stands; a repeated one is not copy.
            try:
                _, markdown = extract_argument_plan(markdown.lstrip())
            except AnswerPageProtocolError:
                pass
        if structured:
            markdown = normalize_search_metadata(markdown, today=reference["today"])
        result = {key: value for key, value in draft.items() if key != "failed_checks"}
        result["markdown"] = markdown
        result["repair"] = {
            "response_id": repaired["response_id"],
            "model": self._responses.model,
            "failed_checks": failed,
            "usage": repaired["usage"],
        }
        problems = answer_page_problems(markdown.encode(), structured=structured)
        if problems:
            result["failed_checks"] = problems
        return result

    @staticmethod
    def build_artifacts(
        *,
        run_id: str,
        source_refs: list[str],
        draft: dict[str, Any],
        artifact_path: str,
        evidence_path: str,
    ) -> tuple[bytes, bytes]:
        markdown = str(draft["markdown"]).encode()
        evidence = (
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "artifact_path": artifact_path,
                    "source_refs": source_refs,
                    "model": draft["model"],
                    "response_id": draft["response_id"],
                    "search_calls": draft["search_calls"],
                    "queries": draft["queries"],
                    "sources": draft["sources"],
                    "citations": draft["citations"],
                    "usage": draft["usage"],
                    **(
                        {"argument_plan": draft["argument_plan"]}
                        if "argument_plan" in draft
                        else {}
                    ),
                    **({"structure": draft["structure"]} if "structure" in draft else {}),
                    **({"repair": draft["repair"]} if "repair" in draft else {}),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        validate_answer_page_artifacts(
            markdown,
            evidence,
            artifact_path=artifact_path,
            evidence_path=evidence_path,
        )
        return markdown, evidence


def page_title(markdown: str) -> str:
    """The page's own H1 as plain words: its name in Decisions, Files and chat."""
    title = re.search(r"(?m)^# (.+)$", markdown)
    return _plain(title.group(1))[:160] if title else ""


def extract_argument_plan(markdown: str) -> tuple[dict, str]:
    match = re.match(r"<!-- tin-answer-plan-v1\s*(\{.*?\})\s*-->\s*", markdown, re.S)
    if not match or len(match.group(1).encode()) > 16000:
        raise AnswerPageProtocolError("answer page lacks a bounded saved argument plan")
    try:
        plan = json.loads(match.group(1))
    except ValueError as exc:
        raise AnswerPageProtocolError("invalid argument plan JSON") from exc
    fields = {"buyer_decision", "positioning", "answer", "proof", "objection", "next_step"}
    if (
        not isinstance(plan, dict)
        or set(plan) != fields
        or any(not isinstance(v, str) or not v.strip() or len(v) > 3000 for v in plan.values())
    ):
        raise AnswerPageProtocolError(
            "argument plan must identify decision, positioning, answer, "
            "proof, objection and next step"
        )
    return plan, markdown[match.end() :].lstrip()


def normalize_search_metadata(markdown: str, *, today: str) -> str:
    """Give the page search-listing frontmatter and a last-updated line that code can trust.

    Code, not the model, owns the quoting and the length limits. A missing or oversized value
    is rebuilt from the page's own title and opening answer instead of failing a paid draft.
    """
    fields: dict[str, str] = {}
    match = re.match(r"\A\s*---[ \t]*\n(.*?)\n---[ \t]*\n+", markdown, re.S)
    if match is not None:
        for line in match.group(1).splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"meta_title", "meta_description"}:
                fields.setdefault(key.strip(), " ".join(value.strip().strip("\"'").split()))
        markdown = markdown[match.end() :]
    page = markdown.lstrip()
    title = re.match(r"# (.+)", page)
    if title is not None and not re.search(
        r"(?m)^Last updated: \d{4}-\d{2}-\d{2}$", re.split(r"(?m)^## ", page)[0]
    ):
        page = f"{title.group(0)}\n\nLast updated: {today}\n{page[title.end() :]}"
    heading = _plain(title.group(1)) if title else ""
    answer = next(
        (
            _plain(block)
            for block in _prose_blocks(re.split(r"(?m)^## ", page)[0])
            if not block.startswith(("# ", "Last updated:"))
        ),
        "",
    )
    meta_title = fields.get("meta_title") or heading
    if len(meta_title) > MAX_META_TITLE:
        meta_title = _clip(meta_title, MAX_META_TITLE)
    low, high = META_DESCRIPTION_RANGE
    description = fields.get("meta_description", "")
    if not low <= len(description) <= high:
        description = _clip(answer, high) if len(answer) >= low else description[:high]
    header = "".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
        for key, value in (("meta_title", meta_title), ("meta_description", description))
    )
    return f"---\n{header}---\n\n{page}"


def _plain(markdown: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    return " ".join(re.sub(r"[`*_]", "", text).split())


def _clip(text: str, limit: int) -> str:
    """Cut at a sentence end when one fits, otherwise at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    sentence = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence >= limit // 2:
        return cut[: sentence + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:-") if " " in cut else text[:limit]


def validate_answer_page(content: bytes, *, structured: bool = False) -> None:
    problems = answer_page_problems(content, structured=structured)
    if problems:
        raise ValueError(problems[0])


def answer_page_problems(content: bytes, *, structured: bool = False) -> list[str]:
    """Every page check the content misses, in the order validation reports them."""
    if not content or len(content) > MAX_ANSWER_PAGE_BYTES:
        return [f"answer page must contain between 1 and {MAX_ANSWER_PAGE_BYTES} bytes"]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ["answer page must be UTF-8 text"]
    problems: list[str] = []
    if structured:
        try:
            text = _validate_search_metadata(text)
        except ValueError as exc:
            problems.append(str(exc))
            match = re.match(r"\A\s*---[ \t]*\n.*?\n---[ \t]*\n+", text, re.S)
            text = text[match.end() :] if match else text
    if not text.startswith("# "):
        problems.append("answer page must begin with a Markdown title")
    if "\n## Sources\n" not in text:
        problems.append("answer page must contain a Sources section")
    if len(re.findall(r"^## ", text, flags=re.MULTILINE)) < 3:
        problems.append("answer page must contain at least three sections")
    if structured:
        problems.extend(_search_structure_problems(text))
    return problems


def _validate_search_metadata(text: str) -> str:
    """Check the frontmatter written by normalize_search_metadata; return the page after it."""
    match = re.match(r'\A---\nmeta_title: (".*")\nmeta_description: (".*")\n---\n\n', text)
    if match is None:
        raise ValueError("answer page must begin with meta_title and meta_description frontmatter")
    title, description = json.loads(match.group(1)), json.loads(match.group(2))
    if not 0 < len(title) <= MAX_META_TITLE:
        raise ValueError(f"answer page meta_title must be 1-{MAX_META_TITLE} characters")
    low, high = META_DESCRIPTION_RANGE
    if not low <= len(description) <= high:
        raise ValueError(f"answer page meta_description must be {low}-{high} characters")
    return text[match.end() :]


def _search_structure_problems(text: str) -> list[str]:
    """Catch the missing pieces of a search-structured page, not matters of taste."""
    problems: list[str] = []
    sections = re.split(r"(?m)^## ", text)
    lead, body = sections[0], sections[1:]
    if not re.search(r"(?m)^Last updated: \d{4}-\d{2}-\d{2}$", lead):
        problems.append("answer page must show a 'Last updated: YYYY-MM-DD' line under its title")
    lead_paragraphs = [
        block
        for block in _prose_blocks(lead)
        if not block.startswith("# ") and not block.startswith("Last updated:")
    ]
    words = len(lead_paragraphs[0].split()) if lead_paragraphs else 0
    low, high = ANSWER_WORDS_RANGE
    if not low <= words <= high:
        problems.append(
            f"answer page must open with a direct answer of about 40-60 words (found {words})"
        )
    headings = [section.split("\n", 1)[0].strip() for section in body]
    if not headings or headings[-1] != "Sources":
        problems.append("answer page must end with its Sources section")
    faq = [
        section
        for section in body
        if re.match(r"(FAQ|Frequently asked questions)\s*$", section.split("\n", 1)[0].strip())
    ]
    if not faq or len(re.findall(r"(?m)^### .+\?\s*$", faq[0])) < 2:
        problems.append("answer page must include an FAQ section with at least two questions")
    questions = [heading for heading in headings if heading.endswith("?")]
    if len(questions) < 2:
        problems.append("answer page must phrase at least two section headings as questions")
    cited = set(re.findall(r"\]\((https?://[^)\s]+)\)", body[-1])) if body else set()
    if len(cited) < MIN_CITED_SOURCES:
        problems.append(f"answer page must list at least {MIN_CITED_SOURCES} sources")
    inline = re.findall(r"\]\((https?://[^)\s]+)\)", "## ".join([lead, *body[:-1]]))
    if len(inline) < MIN_INLINE_CITATIONS:
        problems.append("answer page must cite its sources inline, next to the claims")
    if any(len(block.split()) > MAX_PARAGRAPH_WORDS for block in _prose_blocks(text)):
        problems.append(
            f"answer page paragraphs must stay under {MAX_PARAGRAPH_WORDS} words; split them"
        )
    return problems


def _prose_blocks(text: str) -> list[str]:
    """Paragraphs of running prose: not headings, lists, tables, quotes or code."""
    text = re.sub(r"(?ms)^```.*?^```\s*$", "", text)
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if block and not re.match(r"(#{1,6} |[-*+] |\d+[.)] |\||>|<!--)", block):
            blocks.append(block)
    return blocks


def validate_answer_page_artifacts(
    content: bytes,
    evidence: bytes,
    *,
    artifact_path: str,
    evidence_path: str,
) -> None:
    if not evidence or len(evidence) > MAX_ANSWER_PAGE_EVIDENCE_BYTES:
        raise ValueError(
            "answer-page evidence must contain between 1 and "
            f"{MAX_ANSWER_PAGE_EVIDENCE_BYTES} bytes"
        )
    payload = json.loads(evidence)
    validate_answer_page(content, structured=payload.get("structure") == SEARCH_STRUCTURE)
    if payload.get("artifact_path") != artifact_path:
        raise ValueError("answer-page evidence points to the wrong artifact")
    if not evidence_path.endswith("/evidence.json"):
        raise ValueError("answer-page evidence path is invalid")
    if not isinstance(payload.get("source_refs"), list):
        raise ValueError("answer-page evidence has invalid source references")
    if not isinstance(payload.get("search_calls"), int) or payload["search_calls"] < 1:
        raise ValueError("answer-page evidence has no web search")


def _normalize_response(response: dict[str, Any], *, require_search: bool = True) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, list):
        raise AnswerPageProtocolError("answer-page response has no output list")
    fragments: list[str] = []
    queries: list[str] = []
    sources: list[dict[str, str]] = []
    citations: list[dict[str, str]] = []
    search_calls = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            search_calls += 1
            action = item.get("action")
            if isinstance(action, dict):
                query = action.get("query")
                if isinstance(query, str):
                    queries.append(query)
                action_queries = action.get("queries")
                if isinstance(action_queries, list):
                    queries.extend(value for value in action_queries if isinstance(value, str))
                action_sources = action.get("sources")
                if isinstance(action_sources, list):
                    sources.extend(_normalize_links(action_sources))
        if item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                fragments.append(text)
            annotations = part.get("annotations")
            if isinstance(annotations, list):
                citations.extend(_normalize_links(annotations))
    markdown = "\n".join(fragments).strip()
    if not markdown:
        raise AnswerPageProtocolError("answer-page response returned no Markdown")
    if require_search and search_calls < 1:
        raise AnswerPageProtocolError("answer-page response did not search the web")
    usage = response.get("usage")
    return {
        "response_id": _response_id(response),
        "markdown": markdown + "\n",
        "search_calls": search_calls,
        "queries": _unique_strings(queries),
        "sources": _unique_links(sources)[:MAX_WEB_SOURCES],
        "citations": _unique_links(citations)[:MAX_WEB_SOURCES],
        "usage": usage if isinstance(usage, dict) else {},
    }


def _normalize_links(values: list[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        url = value.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        title = value.get("title")
        result.append({"url": url, "title": title if isinstance(title, str) else ""})
    return result


def _unique_links(values: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if value["url"] in seen:
            continue
        seen.add(value["url"])
        result.append(value)
    return result


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _response_id(response: dict[str, Any]) -> str:
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        raise AnswerPageProtocolError("answer-page response has no ID")
    return response_id
