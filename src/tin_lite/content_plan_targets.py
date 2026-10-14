"""Which planned page serves each target (content-editorial-v10).

A plan opens with this mapping so the founder can check it before anything is drafted: one row
per keyword group from the keyword plan, per page that searchers already see (refresh rows and
Page decisions), and per buyer question from the audit's panel, naming the planned pages that
address it. A target with no page and a page with no target are both listed. The mapping is
computed from the plan and its research, not written by a model:

- a keyword group is served by an item that cites the group, cites one of its keywords, or
  targets one of its keywords' exact search;
- a page target is served by the item that changes that page;
- a competitor the planner chose to write about is served by an item that names it in its
  title;
- a buyer question is served by an item whose title, target search and brief carry at least
  half of the question's distinctive words.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from tin_lite.content_plan_agent import _words

QUESTION_MIN_SHARE = 0.5
# At most this many rows of each kind reach the plan; the rest are counted.
MAX_TARGETS = 60


def _path(value):
    path = urlsplit(value).path if "://" in value else value
    return path.rstrip("/") or "/"


def _volume(keyword):
    observations = keyword.get("observations") or []
    volumes = [o.get("search_volume") for o in observations if o.get("search_volume")]
    impressions = [o.get("impressions") for o in observations if o.get("impressions")]
    return (max(volumes) if volumes else 0), (max(impressions) if impressions else 0)


def _placed(plan):
    """Every planned item with its week, in calendar order."""
    return [
        {**item, "week": batch["id"], "due_date": str(batch["due_date"])}
        for batch in plan["batches"]
        for item in batch["items"]
    ]


def _entry(item, match):
    return {
        "item_id": item["id"],
        "title": item["title"],
        "week": item["week"],
        "format": item.get("format") or item.get("kind") or "article",
        "match": match,
    }


def targets(context, plan, questions=(), competitors=()):
    """The plan's targets, each with the planned items that serve it, and the items no target
    names."""
    research = context.get("research") or {}
    rows = research.get("rows") or []
    keywords = {
        row["data"]["id"]: row["data"]
        for row in rows
        if row["source_id"].startswith("keyword:") and row["data"].get("id")
    }
    items = _placed(plan)
    served = set()
    found = []

    def add(target, matched):
        served.update(entry["item_id"] for entry in matched)
        found.append({**target, "items": matched})

    for row in rows:
        if not row["source_id"].startswith("group:"):
            continue
        group = row["data"]
        members = [keywords[k] for k in group.get("keyword_ids") or [] if k in keywords]
        cited = {row["source_id"]} | {f"keyword:{k['id']}" for k in members}
        searches = {" ".join(str(k.get("keyword", "")).casefold().split()) for k in members}
        volume = sum(_volume(k)[0] for k in members)
        impressions = sum(_volume(k)[1] for k in members)
        primary = keywords.get(group.get("primary_keyword_id") or "", {}).get("keyword")
        matched = []
        for item in items:
            if cited & set(item.get("source_ids") or []):
                matched.append(_entry(item, "cites the group or its keyword"))
            elif " ".join(str(item.get("target_query", "")).casefold().split()) in searches:
                matched.append(_entry(item, "targets one of its searches"))
        add(
            {
                "kind": "keyword_group",
                "id": row["source_id"],
                "label": group.get("title") or primary or row["source_id"],
                "search": primary,
                "keywords": len(members),
                "volume": volume,
                "impressions": impressions,
                "intent": group.get("intent"),
            },
            matched,
        )
    pages = {}
    for row in rows:
        data = row["data"]
        if row["source_id"].startswith("refresh:") and data.get("path"):
            metrics = data.get("metrics") or {}
            entry = pages.setdefault(_path(data["path"]), {"sources": []})
            entry.update(
                impressions=metrics.get("impressions"),
                clicks=metrics.get("clicks"),
                position=metrics.get("position"),
                search=((data.get("searches") or [{}])[0]).get("query"),
            )
            entry["sources"].append(row["source_id"])
        elif (
            row["source_id"].startswith("efficacy:")
            and data.get("decision") in {"refresh", "rewrite"}
            and data.get("path")
        ):
            entry = pages.setdefault(_path(data["path"]), {"sources": []})
            entry["decision"] = data["decision"]
            entry["sources"].append(row["source_id"])
    for path, page in pages.items():
        matched = [
            _entry(item, "changes this page")
            for item in items
            if item.get("destination") and _path(item["destination"]) == path
        ]
        add(
            {
                "kind": "page",
                "id": path,
                "label": path,
                "search": page.get("search"),
                "impressions": page.get("impressions"),
                "clicks": page.get("clicks"),
                "position": page.get("position"),
                "decision": page.get("decision"),
            },
            matched,
        )
    for competitor in competitors:
        name = " ".join(str(competitor.get("name") or "").split())
        if not name or competitor.get("use") == "none":
            continue
        pattern = re.compile(rf"(?<![\w.-]){re.escape(name.casefold())}(?![\w-])")
        matched = [
            _entry(item, "names it") for item in items if pattern.search(item["title"].casefold())
        ]
        add(
            {
                "kind": "competitor",
                "id": f"competitor:{name.casefold()}",
                "label": name,
                "search": None,
                "host": competitor.get("host") or "",
                "use": competitor.get("use") or "",
            },
            matched,
        )
    for number, question in enumerate(questions, 1):
        text = str(question.get("question") or "").strip()
        wanted = _words(text)
        if not wanted:
            continue
        matched = []
        for item in items:
            have = _words(f"{item['title']} {item.get('target_query', '')} {item['brief']}")
            if len(wanted & have) / len(wanted) >= QUESTION_MIN_SHARE:
                matched.append(_entry(item, "answers its words"))
        add(
            {
                "kind": "buyer_question",
                "id": f"question:{number}",
                "label": text[:400],
                "job": question.get("job"),
                "family": question.get("family"),
            },
            matched,
        )
    by_kind = {}
    for target in found:
        by_kind.setdefault(target["kind"], []).append(target)
    kept, omitted = [], 0
    for rows_of_kind in by_kind.values():
        kept += rows_of_kind[:MAX_TARGETS]
        omitted += max(0, len(rows_of_kind) - MAX_TARGETS)
    return {
        "targets": kept,
        "omitted": omitted,
        "unserved": [t["id"] for t in kept if not t["items"]],
        "untargeted": [_entry(item, "") for item in items if item["id"] not in served],
    }


KIND_LABELS = {
    "keyword_group": "Keyword group",
    "page": "Page searchers see",
    "competitor": "Competitor",
    "buyer_question": "Buyer question",
}


def _demand(target):
    if target["kind"] == "keyword_group":
        parts = [f"{target['volume']:,} searches/mo" if target["volume"] else "volume unknown"]
        if target.get("impressions"):
            parts.append(f"{int(target['impressions']):,} impressions")
        return ", ".join(parts)
    if target["kind"] == "page":
        parts = []
        if target.get("impressions"):
            parts.append(f"{int(target['impressions']):,} impressions")
        if target.get("position"):
            parts.append(f"position {target['position']:.1f}")
        if target.get("decision"):
            parts.append(f"Page decisions: {target['decision']}")
        return ", ".join(parts) or "seen in search"
    if target["kind"] == "competitor":
        return target.get("host") or "named by buyers or search"
    return "AI answers"


def markdown_lines(mapping):
    """PLAN.md's opening section: the target-to-page table and both kinds of gap."""
    from tin_lite.keyword_plan import markdown_text

    def cell(value):
        return re.sub(r"\s+", " ", markdown_text(str(value))).replace("|", "\\|")

    targets = mapping["targets"]
    if not targets and not mapping["untargeted"]:
        return []
    served = sum(bool(t["items"]) for t in targets)
    lines = [
        "## Targets and pages",
        "",
        f"{served} of {len(targets)} targets have a planned page; "
        + _pages(len(mapping["untargeted"]), "planned page serves", "planned pages serve")
        + " no target below.",
        "",
    ]
    if targets:
        lines += ["| Target | Kind | Demand | Planned pages |", "| --- | --- | --- | --- |"]
        for target in targets:
            label = target["label"]
            if target.get("search") and target["search"] != label:
                label += f" (“{target['search']}”)"
            pages = "; ".join(
                f"{entry['week'].replace('week_', 'week ')}: {entry['title']}"
                for entry in target["items"]
            )
            lines.append(
                f"| {cell(label)} | {KIND_LABELS[target['kind']]} | {cell(_demand(target))} | "
                f"{cell(pages) if pages else '**No page planned**'} |"
            )
        lines.append("")
        if mapping["omitted"]:
            lines += [f"{mapping['omitted']} more targets are counted but not listed.", ""]
    unserved = [t for t in targets if not t["items"]]
    if unserved:
        lines += ["### Targets with no planned page", ""]
        lines += [f"- {cell(t['label'])} ({KIND_LABELS[t['kind']].lower()})" for t in unserved]
        lines.append("")
    if mapping["untargeted"]:
        lines += ["### Planned pages with no target", ""]
        lines += [
            f"- {entry['week'].replace('week_', 'week ')}: {cell(entry['title'])}"
            for entry in mapping["untargeted"]
        ]
        lines.append("")
    return lines


def _pages(count, one, many):
    return f"{count} {one if count == 1 else many}"


def review_line(mapping, planned):
    """The plan approval's one-line summary in Decisions."""
    targets = mapping["targets"]
    served = sum(bool(t["items"]) for t in targets)
    return (
        f"{planned} planned pages. {served} of {len(targets)} targets have a page"
        + (f", {len(targets) - served} have none" if len(targets) > served else "")
        + (
            "; "
            + _pages(len(mapping["untargeted"]), "page serves", "pages serve")
            + " no listed target"
            if mapping["untargeted"]
            else ""
        )
        + ". Approve to start drafting; nothing is drafted until you do."
    )
