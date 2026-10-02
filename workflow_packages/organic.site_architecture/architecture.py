"""Pure computations for the site architecture plan: inventory, sections, gate, redirects.

No reads and no model here; main.py passes in what it read. Every number keeps its source:
Search Console for 12-month clicks, the traffic snapshot for 28-day sessions, and the organic
audit's summary for click depth, inbound links and possible orphans among the pages it read.
URL depth (path segments) is reported apart; it is not click depth.
"""

import hashlib
import json
import re
from urllib.parse import urlsplit

PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,300}")
# Tokenised and one-time paths never belong in a site plan.
ONE_TIME = re.compile(
    r"/(?:invite|connect|auth|callback|verify|reset|magic)(?:/|$)"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|/[0-9a-f]{24,}(?:/|$)",
    re.I,
)
TYPES = {
    "comparison": re.compile(r"(?:^|/)(?:compare|comparisons?|vs)(?:/|$)|-vs-"),
    "alternative": re.compile(r"alternatives?"),
    "integration": re.compile(r"(?:^|/)integrations?(?:/|$)"),
    "template": re.compile(r"(?:^|/)templates?(?:/|$)"),
    "free tool": re.compile(r"(?:^|/)(?:tools?|free-[a-z-]+|calculators?|generators?)(?:/|$)"),
    "data study": re.compile(r"(?:^|/)(?:research|reports|data|studies|state-of)(?:/|$)"),
}
GROUPS = ("product", "solutions", "resources", "company", "legal", "account", "other")
NAV = ("header", "footer", "both", "none")
PAGE_TYPES = (
    "home",
    "product",
    "feature",
    "pricing",
    "signup",
    "blog",
    "docs",
    "comparison",
    "alternative",
    "integration",
    "template",
    "tool",
    "study",
    "company",
    "legal",
    "utility",
    "other",
)
OWNER = {
    "blog": "content.generate",
    "docs": "content.generate",
    "comparison": "content.plan",
    "alternative": "content.plan",
    "integration": "content.plan",
    "template": "content.plan",
    "tool": "growth.free_tool",
    "study": "content.plan",
}
FOOTER = {"product": "Product", "solutions": "Product", "resources": "Resources"}
FOOTER |= {"company": "Company", "legal": "Legal"}
MAX_REDIRECTS = 20


def site_path(value, host):
    """A site path on `host` (www or not), or None for another site or a malformed URL."""
    text = str(value or "").strip()
    if text.startswith("/"):
        path = text
    else:
        parts = urlsplit(text if "://" in text else "https://" + text)
        other = (parts.hostname or "").lower().removeprefix("www.")
        if other != host.removeprefix("www."):
            return None
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
    return path if PATH.fullmatch(path.split("?")[0]) and ".." not in path else None


def bare(path):
    """The page a path names: no query, no trailing slash except for the home page."""
    return path.split("?")[0].rstrip("/") or "/"


def section_of(path):
    first = bare(path).strip("/").split("/")[0]
    return "/" + first if first else "/"


def url_depth(path):
    return len([p for p in bare(path).strip("/").split("/") if p])


def parse_moves(text):
    """Lines of `old -> new` or a lone new route. Returns (moves, routes, errors)."""
    moves, routes, errors, seen = [], set(), [], {}
    for number, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        old, arrow, new = (part.strip() for part in line.partition("->"))
        if not arrow:
            old, new = None, old
        for value in (old, new):
            if value is not None and (not PATH.fullmatch(value) or ".." in value):
                errors.append(f"line {number}: {value!r} is not a path on this site")
        if old is None:
            routes.add(bare(new))
            continue
        if bare(old) == bare(new):
            errors.append(f"line {number}: {old} points at itself")
        elif old in seen and seen[old] != new:
            errors.append(f"line {number}: {old} is moved twice")
        seen[old] = new
        moves.append((old, new))
        routes.add(bare(new))
    return list(dict.fromkeys(moves)), routes, errors


def redirect_rows(moves, clicks, reason):
    """301 rows for website.change: chains collapsed to the final target, loops refused."""
    target = dict(moves)
    rows, errors = [], []
    for old in target:
        seen, new = {old}, target[old]
        while new in target:
            if new in seen:
                errors.append(f"{old} loops back to itself")
                break
            seen.add(new)
            new = target[new]
        else:
            if new == "/" and bare(old) != "/":
                errors.append(f"{old} would redirect to the home page; name a real equivalent")
                continue
            rows.append(
                {"old": old, "new": new, "status": 301, "reason": f"{reason}: {old} moves to {new}"}
            )
    rows.sort(key=lambda r: -(clicks.get(bare(r["old"])) or 0))
    return rows[:MAX_REDIRECTS], max(0, len(rows) - MAX_REDIRECTS), errors


def sections(inventory):
    """Pages grouped by their first path segment, with totals that keep unknown apart."""
    found = {}
    for page in inventory.values():
        cell = found.setdefault(
            page["section"],
            {
                "id": f"S{len(found) + 1}",
                "prefix": page["section"],
                "pages": 0,
                "clicks_12m": 0,
                "impressions_12m": 0,
                "sessions_28d": 0,
                "deepest": 0,
                "has_root": False,
                "samples": [],
            },
        )
        cell["pages"] += 1
        for key in ("clicks_12m", "impressions_12m", "sessions_28d"):
            cell[key] += page[key] or 0
        cell["deepest"] = max(cell["deepest"], page["depth"])
        cell["has_root"] |= page["path"] == cell["prefix"]
        if len(cell["samples"]) < 5:
            cell["samples"].append(page["path"])
    for cell in found.values():
        cell["samples"] = sorted(cell["samples"], key=lambda p: -(inventory[p]["clicks_12m"] or 0))[
            :5
        ]
    return sorted(found.values(), key=lambda c: (-c["clicks_12m"], c["prefix"]))


def page_type(path):
    for name, pattern in TYPES.items():
        if pattern.search(bare(path).lower()):
            return name
    return None


def gate(ctx):
    """The seven triggers: True fired, False checked and not fired, None not assessed."""
    inventory, audit = ctx["inventory"], ctx["audit"]
    found = []
    found.append(("a", ctx["planned_change"] != "none", "a change is planned"))
    orphans = audit["orphans"] if audit else None
    clicked_orphans = [
        p for p in orphans or [] if (inventory.get(p, {}).get("clicks_12m") or 0) > 0
    ]
    key_orphans = [p for p in orphans or [] if p in ctx["key_pages"]]
    # Only an exact depth counts: an upper bound may hide a shorter path.
    deep = [
        p
        for p in ctx["key_pages"]
        if audit
        and audit["depth"] == "exact"
        and ((audit["rows"].get(p) or {}).get("depth") or 0) > ctx["max_key_depth"]
    ]
    found.append(
        (
            "b",
            bool(clicked_orphans or key_orphans or deep) if orphans is not None else None,
            "a key or clicked page is a possible orphan in the audit's crawl, or a key page sits "
            f"more than {ctx['max_key_depth']} clicks from home"
            + (f": {', '.join(deep[:3])}" if deep else ""),
        )
    )
    by_type = {}
    for path in inventory:
        kind = page_type(path)
        if kind and path != section_of(path):
            by_type.setdefault((kind, section_of(path)), []).append(path)
    unlisted = [
        f"{kind} under {prefix} ({len(paths)} pages)"
        for (kind, prefix), paths in by_type.items()
        if len(paths) >= 3 and prefix not in inventory
    ]
    found.append(
        (
            "c",
            bool(unlisted),
            "three or more pages of one type have no index page"
            + (": " + "; ".join(unlisted[:3]) if unlisted else ""),
        )
    )
    observed = max(len(inventory), (audit or {}).get("sitemap_pages") or 0)
    found.append(("d", observed >= 500, f"about 500 or more URLs are observed ({observed})"))
    deep = [c["prefix"] for c in ctx["sections"] if c["deepest"] >= 3]
    found.append(
        (
            "e",
            None,
            "breadcrumbs in sections three or more levels deep are not assessed without the "
            "site's code" + (f"; deep sections: {', '.join(deep[:5])}" if deep else ""),
        )
    )
    competing = (audit or {}).get("competing") or 0
    merges = [c for c in ctx["decided"] if c.get("kind") == "301"]
    found.append(
        (
            "f",
            bool(competing or merges) if (audit or ctx["decided_read"]) else None,
            "pages compete for one search"
            + (f" ({competing} audit findings, {len(merges)} Page decisions merges)"),
        )
    )
    silent = [
        c["prefix"]
        for c in ctx["sections"]
        if c["impressions_12m"] > 0 and c["clicks_12m"] == 0 and c["pages"] >= 3
    ]
    found.append(
        (
            "g",
            bool(silent) if ctx["clicks_complete"] else None,
            "a section had impressions but no search clicks in 12 months"
            + (f": {', '.join(silent[:3])}" if silent else ""),
        )
    )
    return found


def slash_policy(paths):
    trailing = [p for p in paths if p != "/" and "?" not in p]
    if not trailing:
        return "unknown"
    share = sum(p.endswith("/") for p in trailing) / len(trailing)
    return "trailing slash" if share >= 0.8 else "no trailing slash" if share <= 0.2 else "mixed"


def tree(section_rows, labels):
    lines = ["/"]
    groups = {}
    for cell in section_rows:
        if cell["prefix"] == "/":
            continue
        groups.setdefault(labels[cell["id"]]["group"], []).append(cell)
    for name in GROUPS:
        if name not in groups:
            continue
        lines.append(f"├── {name}")
        for cell in groups[name]:
            label = labels[cell["id"]]["label"]
            hub = "" if cell["has_root"] else " (no index page yet)"
            lines.append(f"│   ├── {label}: {cell['prefix']}{hub}")
            for sample in cell["samples"][:3]:
                if sample != cell["prefix"]:
                    lines.append(f"│   │   └── {sample}")
    return "\n".join(lines)


def url_rules(section_rows, labels, policy):
    rules = {}
    for cell in section_rows:
        if cell["prefix"] == "/":
            continue
        label = labels[cell["id"]]
        slug = "/{slug}" if cell["pages"] > 1 or not cell["has_root"] else ""
        rules[cell["prefix"]] = {
            "pattern": cell["prefix"] + slug,
            "page_type": label["page_type"],
            "parent_hub": cell["prefix"],
            "nav_location": label["nav"],
            "owning_workflow": OWNER.get(label["page_type"], "founder"),
        }
    return {
        "schema": "site_architecture.url_rules/1",
        "sections": rules,
        "words": "lowercase words joined by hyphens; no IDs or dates in the path",
        "trailing_slash": policy,
    }


def block(name, value, *, compact=False):
    text = (
        json.dumps(value, ensure_ascii=False)
        if compact
        else json.dumps(value, indent=1, ensure_ascii=False)
    )
    return f"<!-- {name}.json:start -->\n```json\n{text}\n```\n<!-- {name}.json:end -->"


def read_block(text, name):
    found = re.search(
        rf"<!-- {name}\.json:start -->\s*```json\s*(\{{.*?\}})\s*```\s*<!-- {name}\.json:end -->",
        text or "",
        re.S,
    )
    try:
        return json.loads(found.group(1)) if found else None
    except ValueError:
        return None


def seal(text):
    """Put the SHA-256 of the report, computed with PENDING in both places, into both places."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    return text.replace("Content hash: PENDING", f"Content hash: {digest}").replace(
        '"plan_hash": "PENDING"', f'"plan_hash": "{digest}"'
    ), digest
