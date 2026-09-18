#!/usr/bin/env python3
"""Rank the fifteen marketing systems against one business's parameters.

A weighted fit: every system holds a signed weight against every parameter value, the
business's values fire the matching cells, the cells sum, and the ranking falls out.
Nothing is eliminated; a bad fit is a large negative, never a gate.

    fit = sum(fired cells) / sqrt(cells fired)
          + automation preference   (leverage, centred on 3)
          + speed preference        (log scale, 8 weeks neutral)
          + disposition             (does this suit how the founder likes to work)
          - founder-capacity penalty (hours needed over hours available)
          - already-tried penalty   (-30 properly tested and failed, -8 under-tested)

Usage:
    python3 score.py PROFILE.json          # ranking as JSON on stdout
    python3 score.py --describe            # every parameter and its legal values

PROFILE.json is an object of parameter id -> value, plus an optional "tried" object of
system id -> 0 | 1 | 2. Leave unknown parameters unset; never guess one.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUBRIC = json.loads((HERE / "rubric.json").read_text())
PARAMS = RUBRIC["params"]
SYSTEMS = RUBRIC["systems"]
PARAM_BY_ID = {p["id"]: p for p in PARAMS}
HRS_AVAIL = {"min": 2, "some": 8, "lots": 15}
OUTCOME_LABEL = {
    "leads": "leads",
    "signups": "sign-ups",
    "customers": "paying customers",
    "traffic": "traffic",
    "bookings": "booked calls",
}


def js_round(value: float) -> int:
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def derive_outcome(vals: dict) -> str:
    business = vals.get("businessType", "")
    price = vals.get("priceBand", "")
    if business in {"content", "utility"}:
        return "traffic"
    if business == "agency" or price in {"high", "ent"}:
        return "bookings"
    if price == "free":
        return "signups"
    if vals.get("funnelBreak") in {"discovery", "trust"} and business in {"b2b_saas", "dev_tool"}:
        return "leads"
    return "customers"


def opt_label(param: dict, value: str) -> str:
    return next((label for key, label in param["opts"] if key == value), value)


def score(system: dict, vals: dict, tried: dict) -> dict:
    contribs = []
    for param in PARAMS:
        value = vals.get(param["id"])
        if not value or (isinstance(value, list) and not value):
            continue
        table = system["weights"].get(param["id"])
        if not table:
            continue
        if param.get("multi"):
            values = value if isinstance(value, list) else [value]
            weight = js_round(sum(table.get(item, 0) for item in values) / len(values))
            label = " + ".join(opt_label(param, item) for item in values)
        else:
            weight = table.get(value, 0)
            label = opt_label(param, value)
        if weight:
            contribs.append(
                {"param": param["id"], "label": param["label"], "opt": label, "w": weight}
            )
    outcome = derive_outcome(vals)
    if system["out"] and outcome not in system["out"]:
        contribs.append(
            {"param": "outcome", "label": "Outcome type", "opt": OUTCOME_LABEL[outcome], "w": -16}
        )
    verdict = tried.get(system["id"])
    if verdict == 2:
        contribs.append(
            {"param": "tried", "label": "Already tried", "opt": "tested and failed", "w": -30}
        )
    elif verdict == 1:
        contribs.append(
            {"param": "tried", "label": "Already tried", "opt": "under-tested", "w": -8}
        )
    raw = sum(item["w"] for item in contribs)
    adj = raw / math.sqrt(max(1, len(contribs)))
    leverage = (system["sc"] - 3) * float(vals.get("automatable", 5) or 0)
    if leverage:
        contribs.append(
            {
                "param": "automatable",
                "label": "Automatable",
                "opt": "an agent can run this repeatedly"
                if leverage > 0
                else "a person has to do it each time",
                "w": js_round(leverage),
            }
        )
        adj += leverage
    urgency = float(vals.get("urgency", 6) or 0)
    if urgency:
        speed = -urgency * (math.log2(system["wk"]) - math.log2(8))
        if abs(speed) >= 1:
            contribs.append(
                {
                    "param": "speed",
                    "label": "Speed to signal",
                    "opt": f"about {system['wk']} weeks to a readable result",
                    "w": js_round(speed),
                }
            )
            adj += speed
    likes = vals.get("enjoys") or []
    if likes and system["tr"]:
        hit = [t for t in system["tr"] if t in likes]
        disposition = min(26, 15 * len(hit)) if hit else -14
        contribs.append(
            {
                "param": "enjoys",
                "label": "Suits them",
                "opt": "matches how they like to work" if hit else "not how they like to work",
                "w": disposition,
            }
        )
        adj += disposition
    available = HRS_AVAIL.get(vals.get("hours"))
    if available is not None and system["hrs"] > available:
        over = system["hrs"] - available
        contribs.append(
            {
                "param": "hours",
                "label": "Founder capacity",
                "opt": f"needs {system['hrs']} h/wk, they have ~{available}",
                "w": js_round(-over * 4),
            }
        )
        adj -= over * 4
    contribs.sort(key=lambda item: abs(item["w"]), reverse=True)
    return {"raw": raw, "adj": adj, "contribs": contribs}


def ranked(vals: dict, tried: dict) -> list[dict]:
    rows = [dict(score(system, vals, tried), system=system) for system in SYSTEMS]
    rows.sort(key=lambda item: item["adj"], reverse=True)
    high, low = rows[0]["adj"], rows[-1]["adj"]
    span = (high - low) or 1
    for row in rows:
        row["score"] = js_round(2 + 96 * (row["adj"] - low) / span)
    return rows


def describe() -> str:
    out = []
    for param in PARAMS:
        tier = "required" if param["tier"] == "req" else "optional"
        multi = ", multi-select" if param.get("multi") else ""
        out.append(f"{param['id']} ({tier}{multi}) — {param['label']}")
        for value, label in param["opts"]:
            out.append(f"    {value}: {label}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if "--describe" in argv:
        print(describe())
        return 0
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    profile = json.loads(Path(argv[0]).read_text())
    tried = profile.pop("tried", {}) or {}
    unknown = sorted(set(profile) - set(PARAM_BY_ID))
    if unknown:
        print(f"unknown parameters: {', '.join(unknown)}", file=sys.stderr)
        return 2
    vals = {k: v for k, v in profile.items() if v not in (None, "", [])}
    rows = ranked(vals, tried)
    print(
        json.dumps(
            {
                "outcome": OUTCOME_LABEL[derive_outcome(vals)],
                "ranking": [
                    {
                        "rank": i + 1,
                        "id": r["system"]["id"],
                        "name": r["system"]["name"],
                        "score": r["score"],
                        "raw": r["raw"],
                        "for": [c for c in r["contribs"] if c["w"] > 0][:4],
                        "against": [c for c in r["contribs"] if c["w"] < 0][:4],
                    }
                    for i, r in enumerate(rows)
                ],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
