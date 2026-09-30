# Weights and checks for the prompt panel

Run this reviewed Python block unchanged. `family_weights` sets the four family weights;
`check_panel` recomputes every rule the skill sets on the parsed `prompts.json` block and
returns one line per failure. An empty list means the panel may be delivered as a draft.
Flags (the product or a competitor named) are not failures unless a flag is missing.

```python
import re

STAGES = ("discovery", "comparison", "problem", "buying_intent")
CORE_WEIGHT = 0.40
ADJACENT_BASE = 0.12
FORCING = [
    r"\b(list|name|give me|show me|pick|choose|rank|recommend)\s+(\d+|one|two|three|four|five"
    r"|ten|exactly|only|your top|the top|a single)\b",
    r"\btop\s+\d+\b",
    r"\b\d+\s+(best|top)\b",
    r"\bwhich\b[^?]{0,60}\b(is|are)\s+(the\s+)?best\b",
    r"\b(single|one)\s+best\b",
    r"\bonly\s+(your|the)\s+(top|best)\b",
    r"\bjust\s+one\b",
    r"\band nothing else\b",
]
LIGHT_TAG = [
    r"\bfor (my|our|a) (small |tiny |early[- ]stage |solo |bootstrapped )?(startup|business"
    r"|company|team|agency|saas|side project|app|product|shop|store|site)\b",
    r"\bas an? (solo |technical |non-technical |first-time )?(founder|developer|dev|engineer"
    r"|marketer|indie hacker|small business owner)\b",
    r"\b(i'?m|i am|we'?re|we are) an? \w+",
]
INVENTED = [
    r"\$\s?\d",
    r"\b\d+\s?(k|K)?\s?(usd|dollars|euros|per month|/mo|a month)\b",
    r"\bbudget\b",
    r"\b\d+[- ](person|people|employee|member|seat|user)s?\b",
    r"\bteam of \d+\b",
    r"\b\d+\s+(engineers|developers|employees)\b",
    r"\bby (monday|tuesday|wednesday|thursday|friday|next week|end of (the )?(month|quarter))\b",
    r"\bdeadline\b",
]


def family_weights(impressions):
    """Weights for F1 (core) and the adjacent families F2-F4.

    `impressions` maps each adjacent family ID to the Search Console impressions of the queries
    assigned to it, or None when unknown. The core family always weighs 0.40. Each adjacent
    family gets a 0.12 base plus its share of 0.24 by impressions, so it weighs 0.12-0.36 and
    never outweighs the core; with any count unknown the three split 0.60 evenly.
    """
    ids = sorted(impressions)
    counts = [impressions[k] for k in ids]
    if not all(isinstance(v, int | float) and v >= 0 for v in counts) or not sum(counts):
        return {"F1": CORE_WEIGHT, **{k: round((1 - CORE_WEIGHT) / len(ids), 4) for k in ids}}
    total = sum(counts)
    spread = 1 - CORE_WEIGHT - ADJACENT_BASE * len(ids)
    return {
        "F1": CORE_WEIGHT,
        **{k: round(ADJACENT_BASE + spread * impressions[k] / total, 4) for k in ids},
    }


def words(text):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'.+#/-]*", text))


def name_terms(target, name, aliases):
    host = re.sub(r"^https?://", "", target.lower().strip()).split("/")[0].removeprefix("www.")
    terms = {host, *(a.lower() for a in aliases if a)}
    if name:
        terms.add(name.lower())
    root = host.split(".")[0]
    if len(root) >= 3:
        terms.add(root)
    return terms


def mentions(text, term):
    pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def check_panel(panel, target, competitors=()):
    fails = []
    if panel.get("schema") != "tin.prompt_panel/1":
        fails.append("schema must be tin.prompt_panel/1")
    if panel.get("status") != "draft":
        fails.append("status must be draft; approving in Decisions freezes the panel")
    if not str(panel.get("name") or "").strip():
        fails.append("name the product, from the brand guide or positioning")
    families = panel.get("families") or []
    prompts = panel.get("prompts") or []
    branded = panel.get("branded") or []
    ids = [f.get("id") for f in families]
    if ids != ["F1", "F2", "F3", "F4"]:
        fails.append(f"families must be F1-F4 in order, not {ids}")
    roles = [f.get("role") for f in families]
    if roles != ["core", "adjacent", "adjacent", "adjacent"]:
        fails.append("F1 is the core family and F2-F4 are adjacent")
    if len(families) == 4:
        expected = family_weights({f["id"]: f.get("impressions") for f in families[1:]})
        for family in families:
            weight = family.get("weight")
            if not isinstance(weight, int | float) or abs(weight - expected[family["id"]]) > 0.005:
                fails.append(
                    f"{family.get('id')} weighs {weight}; family_weights gives "
                    f"{expected.get(family.get('id'))}"
                )
    if len(prompts) != 32:
        fails.append(f"{len(prompts)} general prompts; the panel needs 32 (4 families x 8)")
    if len(branded) != 4:
        fails.append(f"{len(branded)} branded prompts; the panel needs 4")
    terms = name_terms(target, panel.get("name"), panel.get("aliases") or [])
    seen = set()
    for fid in ids:
        rows = [p for p in prompts if p.get("family") == fid]
        if len(rows) != 8:
            fails.append(f"family {fid}: {len(rows)} prompts, not 8")
        for stage in STAGES:
            n = sum(1 for p in rows if p.get("stage") == stage)
            if n != 2:
                fails.append(f"family {fid}: {n} {stage} prompts, not 2")
        tags = sum(1 for p in rows if any(re.search(r, p.get("text", ""), re.I) for r in LIGHT_TAG))
        if tags > 2:
            fails.append(f"family {fid}: {tags} prompts carry a context tag; at most 2")
        forms = {"question" if p.get("text", "").rstrip().endswith("?") else "other" for p in rows}
        if len(forms) < 2:
            fails.append(f"family {fid}: every prompt has the same form; mix in fragments")
    for p in prompts:
        text, pid = p.get("text", ""), p.get("id", "?")
        if not 5 <= words(text) <= 15:
            fails.append(f"{pid}: {words(text)} words, outside 5-15: {text}")
        forcing = next((m for r in FORCING if (m := re.search(r, text, re.I))), None)
        if forcing:
            fails.append(f"{pid}: forcing clause ({forcing.group(0)!r}): {text}")
        invented = next((m for r in INVENTED if (m := re.search(r, text, re.I))), None)
        if invented:
            fails.append(f"{pid}: invented budget, team size or deadline ({invented.group(0)!r})")
        if text.lower().strip() in seen:
            fails.append(f"{pid}: duplicate prompt: {text}")
        seen.add(text.lower().strip())
        if any(mentions(text, term) for term in terms):
            fails.append(f"{pid}: names the product outside the branded family: {text}")
        flags = {str(x).lower() for x in p.get("flags") or []}
        for name in competitors:
            if mentions(text, name) and f"competitor:{name.lower()}" not in flags:
                fails.append(f"{pid}: names {name} but carries no competitor:{name} flag")
    for p in branded:
        text, pid = p.get("text", ""), p.get("id", "?")
        if not any(mentions(text, term) for term in terms):
            fails.append(f"{pid}: a branded prompt must name the product: {text}")
        if not p.get("fact_check"):
            fails.append(f"{pid}: branded prompt has no fact check")
        if any(re.search(r, text, re.I) for r in FORCING):
            fails.append(f"{pid}: forcing clause in a branded prompt: {text}")
    for name in (panel.get("sources") or {}).get("competitors") or []:
        if not any(mentions(p.get("text", ""), str(name)) for p in prompts + branded):
            fails.append(f"competitor {name} is listed but appears in no prompt; use or drop it")
    topics = {str(p.get("topic", "")).lower() for p in branded}
    for need in ("pricing", "reviews", "integrations", "versus"):
        if need not in topics:
            fails.append(f"branded prompts have no {need} topic")
    return fails
```
