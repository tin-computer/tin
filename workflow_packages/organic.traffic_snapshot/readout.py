"""The weekly growth readout: fixed sentences filled with numbers the snapshot computed.

It reads nothing itself. main.py passes the week's counts from the same Search Console and
PostHog reads the snapshot makes, so no query runs twice. A change is called only above
`min_count`, with an exact binomial test and Holm's correction across every row tested. Each
decision comes from a fixed rule and names the workflow that acts on it. There is no model call.
"""

import datetime as dt
import math
import statistics

# Hosts that also count as community when they send visitors; shown inside their channel.
COMMUNITY = (
    "news.ycombinator.com",
    "reddit.com",
    "github.com",
    "stackoverflow.com",
    "producthunt.com",
    "dev.to",
    "indiehackers.com",
)
CONTENT = ["/blog/", "/docs/", "/guides/", "/changelog/"]
MAX_ROWS = 12
# The readout's opening line when PostHog is not read, by connection state.
SEARCH_ONLY = {
    "not_connected": "PostHog is not connected, so this readout covers search only: visits, "
    "channels, signups and reading are not measured. Connect PostHog in Integrations to add them.",
    "needs_attention": "PostHog needs attention in Integrations, so this week's readout covers "
    "search only: visits, channels, signups and reading are not measured.",
}


def binomial_p(x1, x2):
    """Exact conditional test for two equal-length windows: x1 ~ Binomial(x1 + x2, 0.5)."""
    n = x1 + x2
    if n == 0:
        return None
    low = sum(math.comb(n, k) for k in range(0, x1 + 1)) / 2**n
    high = sum(math.comb(n, k) for k in range(x1, n + 1)) / 2**n
    return min(1.0, 2 * min(low, high))


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adjusted, floor, m = [1.0] * len(ps), 0.0, len(ps)
    for rank, i in enumerate(order):
        floor = max(floor, min(1.0, (m - rank) * ps[i]))
        adjusted[i] = floor
    return adjusted


def limits(values):
    """Process-behaviour limits (XmR): mean ± 2.66 × mean moving range, from six weeks up."""
    values = [v for v in values if v is not None]
    if len(values) < 6:
        return None
    mean = statistics.mean(values)
    moving = statistics.mean(abs(values[i] - values[i - 1]) for i in range(1, len(values)))
    return mean, mean - 2.66 * moving, mean + 2.66 * moving, len(values)


def pct(x, n):
    return f"{100 * x / n:.1f}%" if n else "n/a"


def span(pair):
    first, end = (dt.date.fromisoformat(str(v)) for v in pair)
    return f"{first.strftime('%-d %b')}–{end.strftime('%-d %b')}"


def community(host):
    return any(host == c or host.endswith("." + c) for c in COMMUNITY)


def build(d):
    """Return (markdown, title, state). `d` holds the week's numbers; None means unknown."""
    weeks, notes = d["weeks"], list(d["notes"])
    now_span, before_span = span(weeks["current"]), span(weeks["prior"])
    min_count = d["min_count"]
    activation, signup, paid_event = d["activation"], d["signup"], d["paid_event"]
    previous = d["previous"] or {}
    channels = d["channels"]  # name -> [sessions, prior, signup sessions, prior] or None
    touch = d["first_touch"]  # label -> counts, or None
    signups = d["signups_week"]  # [week, prior] or None
    # PostHog is optional: without a working connection the readout covers search only.
    posthog = d.get("posthog", "connected")
    measured = posthog == "connected"
    tests = []

    def test(key, x1, x2):
        row = {"key": key, "current": x1, "prior": x2}
        if x1 is None or x2 is None:
            row["call"] = "unknown"
        elif max(x1, x2) < min_count:
            row["call"] = "too little data"
        else:
            row["p"] = binomial_p(x1, x2)
            if row["p"] is None:
                row["call"] = "too little data"
            else:
                tests.append(row)
        return row

    sessions_now = sum(c[0] for c in channels.values()) if channels else None
    sessions_before = sum(c[1] for c in channels.values()) if channels else None
    t_total = test("total signups", *(signups or (None, None)))
    t_sessions = test("total sessions", sessions_now, sessions_before)
    channel_tests = {}
    for name, cell in (channels or {}).items():
        if name == "Internal":
            continue
        channel_tests[name] = (
            test(f"{name} sessions", cell[0], cell[1]),
            test(f"{name} signups", cell[2], cell[3]),
        )
    search_weeks = d["search"]["weeks"]
    week_now = search_weeks[0] if search_weeks else None
    week_before = search_weeks[1] if len(search_weeks) > 1 else None
    t_clicks = test(
        "search clicks",
        week_now["clicks"] if week_now else None,
        week_before["clicks"] if week_before else None,
    )
    for row, adjusted in zip(tests, holm([t["p"] for t in tests]) if tests else [], strict=True):
        row["adj"] = adjusted
        if adjusted < 0.05:
            row["call"] = "change (up)" if row["current"] > row["prior"] else "change (down)"
        else:
            row["call"] = "within normal variation"

    history = previous.get("history") if isinstance(previous.get("history"), dict) else {}
    series_now = {
        "signups": signups[0] if signups else None,
        "sessions": sessions_now,
        "search_clicks": week_now["clicks"] if week_now else None,
    }
    process_notes = []
    for name, value in series_now.items():
        points = [v for _, v in history.get(name, [])]
        bounds = limits(points)
        if bounds and value is not None:
            mean, low, high, n = bounds
            recent = points[-7:] + [value]
            run8 = len(recent) >= 8 and (
                all(v > mean for v in recent) or all(v < mean for v in recent)
            )
            if value < low or value > high or run8:
                process_notes.append(
                    f"{name}: {value} is outside its natural process limits ({low:.0f}–{high:.0f}, "
                    f"{'trial, ' if n < 12 else ''}{n} weeks)"
                )

    # ---------- decisions ----------
    decisions = []
    before_calls = previous.get("calls") or {}
    if signups is not None and signups[0] == 0 and (sessions_now or 0) > 0:
        decisions.append(
            (
                "Check signup tracking",
                f"0 signups this week while {sessions_now} sessions came in (PostHog, {now_span}).",
                "coding agent: confirm the signup event still fires",
            )
        )
    answers = d["answers"]
    if touch:
        total = sum(v["signups"] for v in touch.values())
        direct = touch.get("Direct", {}).get("signups", 0)
        if total and direct / total >= 0.5 and answers is not None and not answers["answered"]:
            decisions.append(
                (
                    'Ask new signups "How did you hear about us?"',
                    f"Direct carries {direct} of {total} first-touch signups over 28 days and no "
                    "signup_source answers were recorded.",
                    "growth.signup_source",
                )
            )
    assistants = d["assistants"]  # name -> [sessions, prior, visitors, signup sessions]
    ai_sessions = (
        sum(v[0] for v in assistants.values())
        if assistants is not None
        else (channels or {}).get("AI assistants", [0])[0]
    )
    audit = d["audit"]
    audit_age = None
    if audit.get("seen_on"):
        audit_age = (d["today"] - dt.date.fromisoformat(audit["seen_on"])).days
    if ai_sessions > 0 and (audit.get("status") != "attached" or (audit_age or 0) > 28):
        age = (
            f"was first read {audit_age} days ago ({audit['seen_on']})."
            if audit_age is not None and audit.get("status") == "attached"
            else "was not found."
        )
        decisions.append(
            (
                "Measure AI visibility again",
                f"{ai_sessions} sessions came from AI assistants this week; the organic audit "
                f"{age}",
                "organic.audit (its buyer questions)",
            )
        )
    for path, clicks, before in d["search"]["falling"][:3]:
        decisions.append(
            (
                "Refresh a falling page",
                f"{path}: {clicks} search clicks in the last 28 days against {before} the 28 "
                "days before (Search Console).",
                "content.refresh",
            )
        )
    for name, (_sessions_test, signups_test) in channel_tests.items():
        signs = []
        if signups_test.get("call") == "within normal variation" and (
            str(before_calls.get(f"{name} signups", "")) == "within normal variation"
        ):
            signs.append("flat weekly signups")
        if activation and touch and name in touch:
            now_cell, before_cell = touch[name]["week_activated"], touch[name]["prior_activated"]
            if (
                before_cell >= min_count
                and now_cell < before_cell
                and signups_test.get("call") in ("within normal variation", "change (up)")
            ):
                p = binomial_p(now_cell, before_cell)
                if p is not None and p < 0.05:
                    signs.append("signup-to-activation softening")
        if len(signs) >= 2:
            decisions.append(
                (
                    f"Decide whether to start the next channel after {name}",
                    "Signs: " + ", ".join(signs) + ".",
                    "founder",
                )
            )
    optional = d["optional_rules"]
    if "platform_dependence" in optional and touch:
        total = sum(v["signups"] for v in touch.values())
        top = max(touch.items(), key=lambda kv: kv[1]["signups"])
        if total and top[1]["signups"] / total >= 0.5:
            decisions.append(
                (
                    "Reduce dependence on one channel",
                    f"{top[0]} carries {top[1]['signups']} of {total} first-touch signups over "
                    "28 days.",
                    "founder",
                )
            )
    if "plateau" in optional and previous.get("plateau_weeks", 0) >= 7:
        if not any(t.get("call", "").startswith("change") for t in tests):
            decisions.append(
                (
                    "Call the plateau",
                    "Eight stored weeks show no called change in total signups or channels.",
                    "founder",
                )
            )

    # ---------- write ----------
    launches = d["launch_dates"]
    first = dt.date.fromisoformat(weeks["current"][0])
    last = dt.date.fromisoformat(weeks["current"][1])
    prior_first = dt.date.fromisoformat(weeks["prior"][0])
    launch_now = [x for x in launches if first <= x <= last]
    launch_prior = [x for x in launches if prior_first <= x < first]
    called = sorted(
        [t for t in tests if t.get("call", "").startswith("change")], key=lambda t: t["adj"]
    )[:2]
    head = []
    if not measured:
        if week_now and week_before:
            head.append(
                f"{week_now['clicks']} search clicks this week against {week_before['clicks']} "
                f"the week before (Search Console, {span([week_now['start'], week_now['end']])}; "
                f"{t_clicks.get('call')})."
            )
        else:
            head.append("Search clicks are unknown this week.")
        head.append(SEARCH_ONLY.get(posthog, SEARCH_ONLY["needs_attention"]))
    elif signups is not None:
        head.append(
            f"{signups[0]} people signed up this week against {signups[1]} the week before "
            f"(PostHog, {now_span} vs {before_span}; n = {signups[0] + signups[1]}; "
            f"{t_total.get('call')})."
        )
    else:
        head.append(
            "Signups are unknown this week"
            + (" because no signup event is set." if not signup else ".")
        )
    if called:
        head.append(
            "Changes called: "
            + "; ".join(
                f"{t['key']} {t['current']} vs {t['prior']} (adjusted p {t['adj']:.3f})"
                for t in called
            )
            + "."
        )
    else:
        head.append("No change was called after correcting for the rows tested.")
    count = len(decisions)
    head.append(
        f"{count} thing{'s' if count != 1 else ''} need{'s' if count == 1 else ''} a decision"
        + (f", starting with: {decisions[0][0].lower()}." if decisions else ".")
    )
    if launch_now or launch_prior:
        which = "this week" if launch_now else "the week before"
        dates = ", ".join(x.isoformat() for x in (launch_now or launch_prior))
        head.append(
            f"A launch happened {which} ({dates}), so the week-over-week calls compare against a "
            "launch week and are not a trend."
        )
    title = (
        f"{called[0]['key'].capitalize()} moved this week"
        if called
        else "Growth this week"
        if measured
        else "Search this week"
    )
    lines = [f"# {title}", "", " ".join(head), "", "## Needs a decision"]
    lines += [f"- **{x[0]}.** Evidence: {x[1]} Owner: {x[2]}." for x in decisions] or [
        "Nothing needs a decision this week."
    ]
    lines.append("")

    if measured:
        ranking = "activated signups" if activation else "any signup (activation is not set)"
        lines += [
            "## Where people came from",
            f"Ranked by first-touch {ranking}. Sessions and signup sessions from PostHog, "
            f"{now_span} vs {before_span}; first-touch signups by the person's first referrer.",
        ]
        if channels:
            lines += [
                "| Channel | Sessions | Signup sessions | First-touch signups | Activated "
                "| Call (sessions) |",
                "|---|---:|---:|---:|---:|---|",
            ]

            def rank_key(item):
                cell = (touch or {}).get(item[0]) or {}
                return (-(cell.get("week_activated" if activation else "week") or 0), -item[1][0])

            for name, cell in sorted(channels.items(), key=rank_key):
                counts = (touch or {}).get(name) if touch is not None else None
                verdict = (
                    "not tested (internal)"
                    if name == "Internal"
                    else channel_tests.get(name, ({}, {}))[0].get("call", "not tested")
                )
                lines.append(
                    f"| {name} | {cell[0]} vs {cell[1]} | {cell[2]} vs {cell[3]} | "
                    f"{(counts or {}).get('week', 0) if touch is not None else 'unknown'} | "
                    f"{(counts or {}).get('week_activated', 0) if touch and activation else 'n/a'} "
                    f"| {verdict} |"
                )
            direct = channels.get("Direct", [0])[0]
            lines.append(
                f"Direct is a measurement gap, not a channel: {direct} sessions this week had no "
                "referrer."
            )
        else:
            lines.append("Channel rows are unknown this week.")
        hosts = d["referrers"]
        if hosts:
            named = [h for h in hosts if h[0] == "Referral" and h[1]][:8]
            lines.append(
                "Named referrers: " + (", ".join(f"{h[1]} {h[2]}" for h in named) or "none") + "."
            )
        lines.append("")

    lines += ["## Search"]
    if week_now and week_before:
        lines.append(
            f"Clicks {span([week_now['start'], week_now['end']])} (Search Console, final data): "
            f"{week_now['clicks']} against {week_before['clicks']} the week before; "
            f"{t_clicks.get('call')}."
        )
        if (
            week_now["impressions"]
            and week_before["impressions"]
            and week_now["position"] is not None
            and week_before["position"] is not None
        ):
            lines.append(
                f"Impressions {week_now['impressions']} vs {week_before['impressions']}; CTR "
                f"{pct(week_now['clicks'], week_now['impressions'])} vs "
                f"{pct(week_before['clicks'], week_before['impressions'])}; position "
                f"{week_now['position']:.1f} vs {week_before['position']:.1f}."
            )
        if (
            t_clicks.get("call") == "change (down)"
            and week_before["impressions"]
            and (week_now["impressions"] or 0) >= week_before["impressions"] * 0.8
        ):
            lines.append(
                "Falling CTR with steady impressions may be AI Overviews, not a ranking loss."
            )
        lines.append(
            "Six weeks of clicks (newest first): "
            + ", ".join(str(w["clicks"]) for w in search_weeks[:6])
            + "."
        )
    else:
        lines.append("Search Console dates were not read; weekly search figures are unknown.")
    split = d["search"]["totals"]
    if split.get("hidden_clicks") is not None:
        brand = split.get("branded_clicks")
        lines.append(
            "Brand split over the last 28 days, listed pages only: "
            + (f"brand searches {brand} clicks; " if brand is not None else "no brand terms; ")
            + f"other searches {split['other_named_clicks']}; hidden (anonymized queries and "
            f"rows past the cap) {split['hidden_clicks']}."
        )
    months = d["search"]["months"]
    if months:
        reliable_from = d["search"]["reliable_from"]
        lines.append(
            "By month: "
            + ", ".join(
                f"{m['month']} {m['clicks']} clicks"
                + ("" if m["reliable"] else f" (impressions unreliable before {reliable_from})")
                for m in sorted(months, key=lambda m: m["month"])[-6:]
            )
            + "."
        )
    if d["search"]["new_pages"]:
        lines.append(
            "Pages new in search over the last 28 days: "
            + ", ".join(d["search"]["new_pages"][:10])
            + "."
        )
    lines.append("")

    if measured:
        lines += ["## AI assistants"]
        if assistants:
            for name, cell in sorted(assistants.items(), key=lambda kv: -kv[1][0]):
                counts = (touch or {}).get(name) or {}
                lines.append(
                    f"- {name}: {cell[0]} sessions vs {cell[1]}; "
                    f"{counts.get('week', 0) if touch is not None else 'unknown'} first-touch "
                    "signups this week."
                )
        else:
            lines.append(
                "No AI assistant sessions were recorded this week"
                + ("." if assistants is not None else " (the referrer read failed).")
            )
        lines.append(
            "Signup answers naming an AI assistant over 28 days: "
            + (
                str(answers["ai"])
                if answers and answers["answered"]
                else "no answers recorded"
                if answers is not None
                else "unknown"
            )
            + "."
        )
        seen = audit.get("seen_on")
        lines.append(f"Organic audit: {'first read ' + seen if seen else 'no audit found'}.")
        lines.append(
            "What stays invisible: app visits without a referrer land in Direct, and AI Overviews "
            "and AI Mode sit inside Search Console's web totals. Branded clicks are the proxy to "
            "watch."
        )
        lines.append("")

        content_paths = d["content_paths"]
        lines += [
            "## Content",
            f"Prefixes: {', '.join(content_paths)}. PostHog, {now_span} vs {before_span}.",
        ]
        content = d["content"]
        if content:
            lines += [
                "| Page | Views | Readers | Median time | >15 s | Read to 90% | "
                "First-touch signups (28 d) |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]

            def share(part, whole):
                if whole >= 5:
                    return pct(part, whole)
                return "too few" if whole else "not tracked"

            for row in content[:MAX_ROWS]:
                if row["median"] is not None and row["timed"] >= 5:
                    median = f"{round(float(row['median']))} s"
                else:
                    median = f"too few ({row['timed']})" if row["timed"] else "not tracked"
                lines.append(
                    f"| {row['path']} | {row['views']} vs {row['views_prior']} | "
                    f"{row['readers']} | "
                    f"{median} | {share(row['over15'], row['timed'])} | "
                    f"{share(row['deep'], row['depth'])} | "
                    f"{row['touches'] if row['touches'] is not None else 'unknown'} |"
                )
            lines.append(
                "Assisted signups (people who read a post before signing up) are not measured yet."
            )
        else:
            lines.append(
                "No views under the content prefixes this week"
                + ("." if content is not None else " (not read).")
            )
        lines.append("")

        lines += ["## Landing pages"]
        landing = d["landing"]
        if landing:
            lines += [
                "| Entry page | Sessions | Signup rate this week | The week before |",
                "|---|---:|---|---|",
            ]
            for path, now_s, before_s, now_su, before_su in landing[:MAX_ROWS]:
                lines.append(
                    f"| {path} | {now_s} vs {before_s} | {now_su} of {now_s} "
                    f"({pct(now_su, now_s)}) "
                    f"| {before_su} of {before_s} ({pct(before_su, before_s)}) |"
                )
        else:
            lines.append("Landing pages are unknown this week.")
        lines.append("")

        lines += ["## Social, community and email"]
        for name in ("Social", "Email"):
            if channels and name in channels:
                lines.append(
                    f"- {name}: {channels[name][0]} sessions vs {channels[name][1]}; "
                    f"{channels[name][2]} signup sessions."
                )
        for _, host, now_s, before_s, _, now_su in [h for h in hosts or [] if community(h[1])][:8]:
            lines.append(
                f"- {host} (community): {now_s} sessions vs {before_s}; {now_su} signup sessions."
            )
        lines.append(
            "Untagged email clicks and shares in Slack, Discord or WhatsApp arrive as Direct, so a "
            "send or posting week can explain a Direct bump. Open rates are left out because Apple "
            "Mail Privacy Protection preloads the pixel."
        )
        lines.append("")

        lines += ["## Paid"]
        if channels and "Paid" in channels:
            lines.append(
                f"Site-side paid sessions {channels['Paid'][0]} vs {channels['Paid'][1]}; signup "
                f"sessions {channels['Paid'][2]} (PostHog)."
            )
        else:
            lines.append(
                "No paid sessions this week (PostHog)."
                if channels
                else "Paid sessions are unknown this week."
            )
        lines.append(
            "Ad platform conversions are never added to site-side counts; ads.monitor reports them."
        )
        lines.append("")

        lines += ["## Funnels and attribution (28 days)"]
        if touch:
            lines += [
                "| First-touch channel | Signups | Activated | Paid |",
                "|---|---:|---:|---:|",
            ]
            for name, value in sorted(touch.items(), key=lambda kv: -kv[1]["signups"]):
                lines.append(
                    f"| {name} | {value['signups']} | "
                    f"{value['activated'] if activation else 'n/a'} | "
                    f"{value['paid'] if paid_event else 'n/a'} |"
                )
            lines.append(
                "Signup answers: "
                + (
                    f"{answers['answered']} people answered a signup-source question"
                    if answers and answers["answered"]
                    else "none recorded"
                )
                + ". Self-reported answers break ties and never make a total. Last-touch "
                "attribution is not read."
            )
        else:
            lines.append(
                "Unknown: "
                + (
                    "no signup event is set."
                    if not signup
                    else "the signup read failed or was cut."
                )
            )
        lines.append("")

    lines += [
        "## Data notes",
        f"Weeks: {weeks['current'][0]} to {weeks['current'][1]} vs {weeks['prior'][0]} to "
        f"{weeks['prior'][1]}, Pacific dates ending on Search Console's last final day; "
        + ("PostHog is read for the same dates." if measured else "PostHog is not read."),
        f"Calls that ran: {', '.join(d['calls']) or 'none'}; no model call. Rows tested: m = "
        f"{len(tests)} (Holm correction).",
    ]
    if measured:
        lines += [
            f"Exclusions: {d['exclusions']}.",
            f"Signup event: {signup or 'not set'}; activation: {activation or 'not set'} within "
            f"{d['activation_days']} days; paid event: {paid_event or 'not set'}.",
        ]
    lines += [
        (
            "Spike weeks: launch dates "
            + ", ".join(x.isoformat() for x in launches)
            + "; weeks holding one are marked."
            if launches
            else "Spike weeks: no launch_dates input, so no week is marked a windfall."
        ),
    ]
    lines += [f"- {n}" for n in notes + process_notes]
    lines.append("")

    def push(name, value):
        points = list(history.get(name, []))
        if points and points[-1][0] == weeks["current"][0]:
            points = points[:-1]  # a rerun in the same week replaces that week's point
        if value is not None:
            points.append([weeks["current"][0], value])
        return points[-26:]

    every_test = [t_total, t_sessions] + [x for pair in channel_tests.values() for x in pair]
    every_test.append(t_clicks)
    state = {
        "week": weeks["current"],
        "spike": bool(launch_now),
        "calls": {t["key"]: t.get("call") for t in every_test},
        "history": {k: push(k, v) for k, v in series_now.items()},
        "plateau_weeks": 0
        if any(t.get("call", "").startswith("change") for t in tests)
        else previous.get("plateau_weeks", 0) + 1,
    }
    return "\n".join(lines), title, state
