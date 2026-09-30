"""The buyer prompt panel a founder approved, as the organic audit's frozen question set.

`organic.prompt_panel` (a Registry package) drafts 32 buyer prompts in four intent families,
weighted so the product's own category leads, and the founder reviews the draft in
Decisions. Approving it freezes it: audit policy organic-audit-v11 then asks those questions
instead of drafting its own. The audit's cost bound stays the same, so it asks at most
`max_questions` of them, allocated to the families by weight.

Pure parsing and selection; organic_audit_panel.founder_panel reads the approved run.
"""

from __future__ import annotations

import json
import re

WORKFLOW_KEY = "organic.prompt_panel"
PANEL_PATH = "reports/research/prompt-panel/PROMPT_PANEL.md"
SCHEMA = "tin.prompt_panel/1"
MAX_BYTES = 64_000
# The panel's stages, and the audit question family each one asks.
STAGES = {
    "discovery": "discovery",
    "comparison": "comparison",
    "problem": "problem",
    "buying_intent": "constraint",
}
BLOCK = re.compile(
    r"<!-- prompts\.json:start -->\s*```(?:json)?\s*(\{.*?\})\s*```\s*<!-- prompts\.json:end -->",
    re.S,
)


def host_of(value: str) -> str:
    text = re.sub(r"^https?://", "", str(value or "").strip().lower()).split("/")[0]
    return text.removeprefix("www.")


def parse(raw: str | bytes | None) -> dict | None:
    """The panel block, or None when the report has none or it is malformed."""
    if raw is None or len(raw) > MAX_BYTES:
        return None
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    found = BLOCK.search(text)
    try:
        panel = json.loads(found.group(1)) if found else None
    except ValueError:
        return None
    if not isinstance(panel, dict) or panel.get("schema") != SCHEMA:
        return None
    families = panel.get("families")
    prompts = panel.get("prompts")
    if not isinstance(families, list) or not isinstance(prompts, list) or not families:
        return None
    return panel


def allocate(families: list[dict], slots: int) -> dict[str, int]:
    """Largest-remainder seats per family by weight; the core family always gets one."""
    ids = [f.get("id") for f in families if isinstance(f.get("id"), str)]
    weights = {
        f["id"]: float(f["weight"])
        for f in families
        if isinstance(f.get("id"), str) and isinstance(f.get("weight"), int | float)
    }
    if len(weights) != len(ids) or sum(weights.values()) <= 0:
        weights = dict.fromkeys(ids, 1.0)
    total = sum(weights.values())
    exact = {fid: slots * weights[fid] / total for fid in ids}
    seats = {fid: int(exact[fid]) for fid in ids}
    for fid in sorted(ids, key=lambda f: (-(exact[f] - seats[f]), ids.index(f))):
        if sum(seats.values()) >= slots:
            break
        seats[fid] += 1
    core = next((f["id"] for f in families if f.get("role") == "core"), None)
    if core in seats and seats[core] == 0 and slots:
        donor = max(ids, key=lambda f: (seats[f], -ids.index(f)))
        seats[donor] -= 1
        seats[core] = 1
    return seats


def questions(panel: dict, max_questions: int, site_url: str) -> list[dict]:
    """The audit's questions: each family's prompts in stage order, up to its seats."""
    families = [f for f in panel["families"] if isinstance(f, dict)]
    seats = allocate(families, min(max_questions, len(panel["prompts"])))
    chosen = []
    for family in families:
        rows = [
            p
            for p in panel["prompts"]
            if isinstance(p, dict)
            and p.get("family") == family.get("id")
            and p.get("stage") in STAGES
            and 20 <= len(str(p.get("text") or "")) <= 400
        ]
        order = list(STAGES)
        rows.sort(key=lambda p: (order.index(p["stage"]), str(p.get("id"))))
        # Take one prompt per stage before a second of any stage.
        picked, used = [], set()
        for round_ in range(2):
            for row in rows:
                if len(picked) >= seats.get(family.get("id"), 0):
                    break
                if row["stage"] in used and round_ == 0:
                    continue
                if row in picked:
                    continue
                picked.append(row)
                used.add(row["stage"])
        name = str(family.get("name") or family.get("id"))[:200]
        chosen += [
            {
                "job": name if len(name) >= 5 else f"Buyers looking for {name}",
                "family": STAGES[row["stage"]],
                "question": str(row["text"]).strip(),
                "fit_reason": f"From the approved buyer prompt panel, family {family.get('id')} "
                f"({name}), prompt {row.get('id')}.",
                "source_url": site_url,
            }
            for row in picked
        ]
    return chosen


def identity(panel: dict, host: str) -> dict:
    """Name, aliases and competitors for grading answers against this panel."""
    name = str(panel.get("name") or "").strip() or host.split(".")[0].capitalize()
    aliases = [str(a).strip() for a in panel.get("aliases") or [] if str(a).strip()][:5]
    competitors = [
        str(c).strip()
        for c in (panel.get("sources") or {}).get("competitors") or []
        if 2 <= len(str(c).strip()) <= 120
    ][:10]
    return {"name": name[:120], "aliases": aliases, "competitor_names": competitors}
