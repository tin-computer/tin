"""Check a content portfolio before finishing: python3 check_portfolio.py PORTFOLIO.md BRIEF_DIR

Prints every problem Tin would leave an item out for, or a week it would leave empty, and exits 1
when there is anything to fix. Standard library only.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

START = "<!-- content-portfolio.json:start -->"
END = "<!-- content-portfolio.json:end -->"
FORMATS = {
    "alternative",
    "comparison",
    "roundup",
    "workaround",
    "answer",
    "family_hub",
    "family_page",
    "use_case",
    "guide",
    "refresh",
    "update",
}
UPDATES = {"refresh", "update"}
STRENGTHS = {"measured", "inferred", "bet"}
PREFIXES = ("keyword:", "group:", "audit:", "refresh:", "efficacy:", "competitor:", "page:")


def norm(text):
    return " ".join(str(text or "").casefold().split())


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").casefold()).strip("-")


def main(portfolio_path, brief_dir):
    brief = Path(brief_dir)
    root = brief
    for _ in range(4):  # reports/content-plan/<run>/brief
        root = root.parent
    context = json.loads((brief / "context.json").read_text())
    research = json.loads((brief / "research.json").read_text())
    pages = json.loads((brief / "pages.json").read_text())
    program = context["program"]
    host = program["host"]
    known = {row["source_id"] for row in research.get("rows", [])}
    known |= {p["source_id"] for p in pages.get("pages", []) if p.get("source_id")}
    site = {p["path"].rstrip("/") or "/": p for p in pages.get("site_pages", [])}
    for page in pages.get("pages", []):
        if page.get("status") == "inspected":
            site.setdefault(urlsplit(page["url"]).path.rstrip("/") or "/", {"path": page["url"]})
    decisions = ((context.get("site_signals") or {}).get("page_decisions")) or {}
    blocked = set(decisions.get("keep") or []) | set((decisions.get("cut") or {}).keys())
    titles = {norm(p.get("title")) for p in site.values() if p.get("title")}
    slugs = {p.rstrip("/").rsplit("/", 1)[-1] for p in site}

    text = Path(portfolio_path).read_text()
    errors, notes = [], []
    if text.count(START) != 1 or text.count(END) != 1:
        print("The portfolio needs exactly one block between the two marker lines.")
        return 1
    body = text.split(START, 1)[1].split(END, 1)[0].strip()
    body = re.sub(r"^```(?:json)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body)
    try:
        data = json.loads(body)
    except ValueError as exc:
        print(f"The block is not valid JSON: {exc}")
        return 1
    if data.get("schema") != "content-portfolio/1":
        errors.append('schema must be "content-portfolio/1"')
    if not str(data.get("strategy") or "").strip():
        errors.append("strategy is empty")
    items = data.get("opportunities") or []
    if not isinstance(items, list) or not items:
        errors.append("opportunities is empty")
        items = []
    editable = {item["id"] for item in context.get("existing_items", []) if item["editable"]}
    seen_titles, seen_queries, seen_pages, ids = {}, {}, {}, set()
    usable = 0
    for index, item in enumerate(items, 1):
        label = f"#{index} {str(item.get('title') or '')[:60]!r}"
        problems = []
        if not str(item.get("title") or "").strip() or not str(item.get("brief") or "").strip():
            problems.append("needs a title and a brief")
        if len(str(item.get("brief") or "")) > 1800:
            notes.append(f"{label}: brief is over 1,800 characters and will be cut")
        fmt, action = item.get("format"), item.get("action")
        if fmt not in FORMATS:
            problems.append(f"format must be one of {sorted(FORMATS)}")
        if action not in {"new_page", "update_page"}:
            problems.append("action must be new_page or update_page")
        if fmt in UPDATES and action != "update_page":
            problems.append("a refresh or update is an update_page")
        if fmt in FORMATS - UPDATES and action == "update_page":
            notes.append(f"{label}: an update_page becomes format 'update'")
        if item.get("evidence_strength") not in STRENGTHS:
            problems.append("evidence_strength must be measured, inferred or bet")
        if item.get("id") in ids:
            problems.append("repeats an id")
        ids.add(item.get("id"))
        destination = str(item.get("destination") or "")
        if action == "update_page":
            path = destination
            if destination.startswith("http"):
                parsed = urlsplit(destination)
                if (parsed.hostname or "").removeprefix("www.") != host.removeprefix("www."):
                    problems.append(f"destination must be on {host}")
                path = parsed.path
            path = path.rstrip("/") or "/"
            if path not in site:
                problems.append(f"{path} is not in pages.json (site_pages or read pages)")
            if path in blocked:
                problems.append(f"Page decisions keeps, merges or retires {path}")
            if path in seen_pages:
                problems.append(f"{path} is already changed by {seen_pages[path]}")
            seen_pages[path] = label
        elif item.get("id") not in editable:
            if norm(item.get("title")) in titles or slug(item.get("title")) in slugs:
                problems.append("the site already has a page with this title or address")
        key = norm(item.get("title"))
        if key in seen_titles:
            problems.append(f"repeats the title of {seen_titles[key]}")
        seen_titles[key] = label
        query = norm(item.get("target_query"))
        if query and query in seen_queries:
            notes.append(f"{label}: same target_query as {seen_queries[query]}")
        if query:
            seen_queries[query] = label
        sources = item.get("sources") or []
        for source in sources if isinstance(sources, list) else []:
            source = str(source)
            if source.startswith(PREFIXES):
                if source not in known:
                    problems.append(f"unknown source_id {source}")
            elif source.startswith("file:"):
                path = root / source.removeprefix("file:")
                if not path.is_file():
                    problems.append(f"no project file {source}")
                elif path.stat().st_size > 20_000:
                    notes.append(f"{label}: {source} is over 20 KB; the writer will not load it")
            elif not source.startswith("https://"):
                problems.append(f"source {source!r} is not a source_id, file: path or https URL")
        if not sources:
            notes.append(f"{label}: no sources")
        if not item.get("verification"):
            notes.append(f"{label}: no verification checks")
        if problems:
            errors += [f"{label}: {p}" for p in problems]
        else:
            usable += 1
    excluded = " ".join(str(x) for x in data.get("excluded") or [])
    for path in [*(decisions.get("refresh") or {}), *(decisions.get("rewrite") or {})]:
        if (path.rstrip("/") or "/") not in seen_pages and path not in excluded:
            notes.append(
                f"Page decisions marks {path} for a refresh or rewrite: plan it or say why not "
                "in excluded (Tin adds it after your items otherwise)"
            )
    slots = program["slots"]
    changes = sum(item.get("action") == "update_page" for item in items)
    if items and changes * 2 > len(items):
        notes.append(
            f"{changes} of {len(items)} items change existing pages; plan more new pages unless "
            "the strategy explains why the site has little new to add"
        )
    if usable < slots:
        notes.append(
            f"{usable} usable items for {slots} slots: weeks after the last item stay empty. "
            "Fill them unless gaps explains why there is nothing honest left to plan."
        )
    for line in errors:
        print("ERROR", line)
    for line in notes:
        print("NOTE ", line)
    print(f"{usable} usable of {len(items)} opportunities; {slots} slots.")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
