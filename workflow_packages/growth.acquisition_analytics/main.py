"""Weekly acquisition report: where visitors and signups came from, search with a brand split,
AI assistants, content and landing pages, and which week-over-week changes are real.

Every count is a bounded aggregate read (PostHog HogQL or Search Console) or comes from the
traffic snapshot. A change is called only above `min_count`, with an exact binomial test and
Holm's correction across every row tested. Prose comes from fixed sentence templates filled
with computed values; there is no model call. Names from project files and inputs become
quoted literals, never SQL.

Channels are the shared map in channels.py (the same as product.analytics_brief's).
"""

import datetime as dt
import json
import math
import re
import statistics
import time

from channels import ASSISTANTS, assistant, classify, domain

OUT = "analytics/GROWTH_ANALYTICS.md"
SNAPSHOT = "analytics/traffic-snapshot.json"
AI_AUDIT = "reports/AI_VISIBILITY.md"
BUDGET_SECONDS = 40
EVENT = re.compile(r"[A-Za-z0-9_$:. -]{1,128}")
PROPERTY = re.compile(r"(?:person:)?[A-Za-z_$][A-Za-z0-9_$]{0,63}")
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
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
# Sign-in and checkout referrers are steps inside the product, not channels.
NOT_CHANNELS = (
    "NOT (ref IN ('localhost', 'accounts.google.com', 'checkout.stripe.com') "
    "OR startsWith(ref, 'localhost:') OR startsWith(ref, '127.0.0.1') OR startsWith(ref, 'login.'))"
)


# ---------- helpers ----------


def literal(value):
    text = str(value)
    if len(text) > 300 or any(ord(char) < 32 for char in text):
        raise ValueError("unsafe literal")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def reference(name):
    if name.startswith("person:"):
        return "person.properties[" + literal(name[7:]) + "]"
    return "properties[" + literal(name) + "]"


def event_name(value):
    text = str(value or "").strip()
    return text if EVENT.fullmatch(text) else None


def iso(day):
    return day.isoformat()


def span(first, end):
    return f"{first.strftime('%-d %b')}–{(end - dt.timedelta(days=1)).strftime('%-d %b')}"


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


def read_text(ctx, path, notes, quiet=False):
    try:
        return ctx.files.read_text(path)
    except FileNotFoundError:
        if not quiet:
            notes.append(f"{path} was not found.")
    except (ValueError, OSError) as exc:
        notes.append(f"{path} could not be read ({str(exc)[:80]}).")
    return None


def exclusions(domains, flag):
    """Team exclusions plus the product's own sign-in and checkout referrers.

    Returns (SQL fragment starting with AND, plain words, unsupported items)."""
    rules, words, unsupported = [], [], []
    email = "lower(ifNull(toString(person.properties['email']), ''))"
    for item in list(domains or [])[:10]:
        text = str(item).strip().lower().lstrip("@")
        if DOMAIN.fullmatch(text):
            rules.append(
                f"NOT (endsWith({email}, {literal('@' + text)}) OR "
                f"endsWith({email}, {literal('.' + text)}))"
            )
            words.append(f"emails at {text}")
        else:
            unsupported.append(f"email domain {text}")
    flag = str(flag or "").strip()
    if flag and PROPERTY.fullmatch(flag):
        rules.append(f"NOT (lower(ifNull(toString({reference(flag)}), '')) IN ('true', '1'))")
        words.append(f"{flag} true")
    elif flag:
        unsupported.append(f"internal flag {flag}")
    ref = "lower(ifNull(toString(properties['$referring_domain']), ''))"
    rules.append(NOT_CHANNELS.replace("ref", ref))
    words.append("referrers localhost, accounts.google.com, checkout.stripe.com and login.*")
    return " AND " + " AND ".join(rules), words, unsupported


def label(medium, referrer, source, own):
    """A report row label: the channel, with each named AI assistant on its own row."""
    channel = classify(medium, referrer, own, source=source)
    if channel == "AI assistants":
        return assistant(referrer) or "AI assistants (other)", domain(referrer)
    return channel, domain(referrer) if referrer else domain(source)


AI_ROWS = set(ASSISTANTS.values()) | {"AI assistants (other)"}


def is_ai(name):
    return name in AI_ROWS


def community(host):
    return any(host == c or host.endswith("." + c) for c in COMMUNITY)


# ---------- main ----------


async def run(ctx, inputs):
    started = time.monotonic()
    notes, calls, skipped = [], [], []
    today = dt.datetime.now(dt.UTC).date()
    current_start = today - dt.timedelta(days=7)
    prior_start = today - dt.timedelta(days=14)
    month_start = today - dt.timedelta(days=28)
    min_count = int(inputs.get("min_count") or 20)
    optional = set(inputs.get("optional_rules") or [])
    launches = []
    for value in inputs.get("launch_dates") or []:
        try:
            launches.append(dt.date.fromisoformat(str(value)[:10]))
        except ValueError:
            notes.append(f"launch date {str(value)[:10]} is not a date; ignored.")
    launch_now = [d for d in launches if current_start <= d < today]
    launch_prior = [d for d in launches if prior_start <= d < current_start]

    # 1. The traffic snapshot: its definitions fill in anything not given here.
    snap = None
    snap_text = read_text(ctx, SNAPSHOT, notes)
    if snap_text:
        try:
            snap = json.loads(snap_text)
        except ValueError:
            notes.append(f"{SNAPSHOT} is not valid JSON.")
    definitions = (snap or {}).get("definitions") or {}
    snap_date = None
    if snap:
        try:
            snap_date = dt.date.fromisoformat(str(snap.get("generated_at"))[:10])
        except ValueError:
            snap_date = None
    snap_fresh = bool(snap_date and (today - snap_date).days <= 8)
    if snap and not snap_fresh:
        notes.append(f"stale: traffic snapshot, last data {snap_date or 'unknown'}")
    prev_text = read_text(ctx, OUT, notes, quiet=True)
    stored_prev = {}
    if prev_text:
        found = re.search(r"## Stored numbers\s*```json\s*(\{.*?\})\s*```", prev_text, re.S)
        if found:
            try:
                stored_prev = json.loads(found.group(1))
            except ValueError:
                stored_prev = {}

    signup = event_name(inputs.get("signup_event") or definitions.get("signup_event"))
    activation = event_name(inputs.get("activation_event") or definitions.get("activation_event"))
    paid_event = event_name(inputs.get("paid_event"))
    window_hours = 24 * int(
        inputs.get("activation_window_days") or definitions.get("activation_window_days") or 7
    )
    window_hours = min(max(window_hours, 1), 24 * 30)
    if not activation:
        notes.append("No activation event is set, so channels are ranked by any signup.")

    snapshot_rules = definitions.get("exclusions") or []
    domains = inputs.get("exclude_email_domains")
    if domains is None:
        domains = next(
            (r.get("value") for r in snapshot_rules if r.get("property") == "person:email"), []
        )
    flag = inputs.get("internal_flag_property")
    if flag is None:
        flag = next((r.get("property") for r in snapshot_rules if r.get("op") == "truthy"), "")
    ex, ex_words, unsupported = exclusions(domains, flag)
    for item in unsupported:
        notes.append(f"unsupported exclusion: {item} (listed, not applied)")
    if not domains and not flag:
        notes.append("No team email domains or internal flag are set, so team visits count.")

    own = [
        str(h).lower()
        for h in inputs.get("website_hosts") or definitions.get("website_hosts") or []
    ]
    brand = [str(b) for b in inputs.get("brand_terms") or definitions.get("brand_terms") or []][:20]
    content_paths = [
        p
        for p in inputs.get("content_paths") or CONTENT
        if isinstance(p, str) and p.startswith("/")
    ][:8]
    reliable_from = inputs.get("impressions_reliable_from") or definitions.get(
        "impressions_reliable_from"
    )
    reliable_from = dt.date.fromisoformat(str(reliable_from)[:10]) if reliable_from else None

    async def call(step, service, operation, arguments):
        if time.monotonic() - started > BUDGET_SECONDS:
            skipped.append(f"{step}, time budget reached")
            return None
        try:
            response = await ctx.services.call(
                service=service, step=step, operation=operation, arguments=arguments
            )
        except ValueError as exc:
            if str(exc).startswith("The service request differs from its declared contract"):
                raise
            skipped.append(f"{step}, {str(exc)[:120]}")
            return None
        if not isinstance(response, dict) or not isinstance(response.get("rows"), list):
            skipped.append(f"{step}, malformed response")
            return None
        rows = response["rows"]
        if rows and isinstance(rows[0], list):
            if not isinstance(response.get("columns"), list):
                skipped.append(f"{step}, rows without columns")
                return None
            rows = [dict(zip(response["columns"], row, strict=False)) for row in rows]
        calls.append(step)
        partial = bool(
            response.get("truncated") or response.get("has_more") or response.get("next_start_row")
        )
        return {"rows": rows, "partial": partial}

    # 2. PostHog reads (aggregates only).
    scope = ""
    if own:
        scope = (
            " AND ("
            + " OR ".join(
                f"lower(ifNull(toString(properties['$host']), '')) = {literal(h)}" for h in own
            )
            + ")"
        )
    is_signup = f"event = {literal(signup)}" if signup else "0"
    medium = "lower(ifNull(toString(properties['utm_medium']), ''))"
    referrer = "lower(ifNull(toString(properties['$referring_domain']), ''))"
    source = "lower(ifNull(toString(properties['utm_source']), ''))"
    window = (
        f"timestamp >= toDateTime({literal(iso(prior_start))}) "
        f"AND timestamp < toDateTime({literal(iso(today))})"
    )
    sessions_sql = (
        "SELECT toString(properties['$session_id']) AS sid, any(distinct_id) AS did, "
        f"if(min(timestamp) >= toDateTime({literal(iso(current_start))}), 'current', 'prior') "
        f"AS period, argMin({medium}, timestamp) AS medium, "
        f"argMin({referrer}, timestamp) AS ref, argMin({source}, timestamp) AS src, "
        "argMinIf(toString(properties['$pathname']), timestamp, event = '$pageview') AS path, "
        f"countIf(event = '$pageview') AS pv, if(countIf({is_signup}) > 0, 1, 0) AS su "
        f"FROM events WHERE {window} AND (event = '$pageview'{scope} OR {is_signup}){ex} "
        "AND notEmpty(ifNull(toString(properties['$session_id']), '')) GROUP BY sid HAVING pv > 0"
    )
    p1 = await call(
        "P1",
        "posthog",
        "query.hogql",
        {
            "name": "channels",
            "query": "SELECT period, medium, ref, src, count() AS sessions, "
            f"uniqExact(did) AS visitors, sum(su) AS signups FROM ({sessions_sql}) "
            "GROUP BY period, medium, ref, src ORDER BY sessions DESC LIMIT 1000",
        },
    )

    p2 = None
    if signup:
        activated = (
            "arrayMin(arrayFilter(x -> x >= s AND x <= s + "
            f"{window_hours * 3600}, groupArrayIf(toUnixTimestamp(timestamp), "
            f"event = {literal(activation)}))) > 0"
            if activation
            else "0"
        )
        paid = (
            f"arrayMin(arrayFilter(x -> x >= s AND x <= s + {28 * 86400}, "
            f"groupArrayIf(toUnixTimestamp(timestamp), event = {literal(paid_event)}))) > 0"
            if paid_event
            else "0"
        )
        names = ", ".join(
            literal(e)
            for e in dict.fromkeys(
                [signup]
                + ([activation] if activation else [])
                + ([paid_event] if paid_event else [])
            )
        )
        first = "lower(ifNull(toString(person.properties['$initial_{0}']), ''))"
        per_person = (
            f"SELECT person_id, minIf(toUnixTimestamp(timestamp), event = {literal(signup)}) AS s, "
            f"{activated} AS act, {paid} AS paid, any({first.format('utm_medium')}) AS fmed, "
            f"any({first.format('referring_domain')}) AS fref, "
            f"any({first.format('utm_source')}) AS fsrc, "
            f"argMinIf({referrer}, timestamp, event = {literal(signup)}) AS lref, "
            "any(lower(trim(ifNull(toString(person.properties['signup_source']), "
            "ifNull(toString(person.properties['self_reported_source']), ''))))) AS answer "
            f"FROM events WHERE timestamp >= toDateTime({literal(iso(month_start))}) "
            f"AND timestamp < toDateTime({literal(iso(today))}) "
            f"AND event IN ({names}){ex} GROUP BY person_id HAVING s > 0"
        )

        def epoch(day):
            return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp())

        p2 = await call(
            "P2",
            "posthog",
            "query.hogql",
            {
                "name": "signups by first touch",
                "query": f"SELECT multiIf(s >= {epoch(current_start)}, 'current', "
                f"s >= {epoch(prior_start)}, 'prior', 'earlier') AS period, fmed, fref, fsrc, "
                "lref, substring(answer, 1, 60) AS answer, count() AS signups, "
                f"sum(act) AS activated, sum(paid) AS paid FROM ({per_person}) "
                "GROUP BY period, fmed, fref, fsrc, lref, answer ORDER BY signups DESC LIMIT 1000",
            },
        )
    else:
        skipped.append("P2, no signup event is set")

    prefixes = " OR ".join(f"startsWith(pth, {literal(p)})" for p in content_paths)
    p3 = await call(
        "P3",
        "posthog",
        "query.hogql",
        {
            "name": "content reading",
            "query": "SELECT period, pth, countIf(event = '$pageview') AS views, "
            "uniqExactIf(did, event = '$pageview') AS readers, "
            "quantileExact(0.5)(if(event = '$pageleave', dur, NULL)) AS median_s, "
            "countIf(event = '$pageleave' AND dur IS NOT NULL) AS with_dur, "
            "countIf(event = '$pageleave' AND dur > 15) AS over15, "
            "countIf(event = '$pageleave' AND dep IS NOT NULL) AS with_dep, "
            "countIf(event = '$pageleave' AND if(dep > 1, dep / 100, dep) >= 0.9) AS deep FROM ("
            "SELECT event, distinct_id AS did, "
            f"if(timestamp >= toDateTime({literal(iso(current_start))}), 'current', 'prior') "
            "AS period, if(event = '$pageleave', "
            "ifNull(toString(properties['$prev_pageview_pathname']), "
            "toString(properties['$pathname'])), toString(properties['$pathname'])) AS pth, "
            "toFloat64OrNull(toString(properties['$prev_pageview_duration'])) AS dur, "
            "toFloat64OrNull(toString(properties['$prev_pageview_max_content_percentage'])) AS dep "
            f"FROM events WHERE {window} AND event IN ('$pageview', '$pageleave'){scope}{ex}) "
            f"WHERE {prefixes or '0'} GROUP BY period, pth ORDER BY views DESC LIMIT 1000",
        },
    )
    p4 = await call(
        "P4",
        "posthog",
        "query.hogql",
        {
            "name": "landing pages",
            "query": f"SELECT period, path, count() AS sessions, sum(su) AS signups FROM "
            f"({sessions_sql}) GROUP BY period, path ORDER BY sessions DESC LIMIT 500",
        },
    )

    # 3. Search Console.
    g1 = await call(
        "G1",
        "gsc",
        "search_analytics.read",
        {
            "start_date": iso(today - dt.timedelta(days=366)),
            "end_date": iso(today - dt.timedelta(days=1)),
            "dimensions": ["date"],
            "row_limit": 400,
            "start_row": 0,
            "dimension_filters": [],
        },
    )
    days = {}
    for row in (g1 or {}).get("rows", []):
        try:
            days[dt.date.fromisoformat(str((row.get("keys") or [""])[0]))] = row
        except ValueError:
            continue
    gsc_end = max(days) if days else None
    if gsc_end and (today - gsc_end).days > 5:
        notes.append(f"stale: Search Console, last data {gsc_end}")
    weeks_gsc = []
    if gsc_end:
        weeks_gsc = [
            (gsc_end - dt.timedelta(days=6), gsc_end),
            (gsc_end - dt.timedelta(days=13), gsc_end - dt.timedelta(days=7)),
        ]
    g2 = g3 = None
    if weeks_gsc:
        base = {
            "start_date": iso(weeks_gsc[0][0]),
            "end_date": iso(weeks_gsc[0][1]),
            "start_row": 0,
            "dimension_filters": [],
        }
        g2 = await call(
            "G2",
            "gsc",
            "search_analytics.read",
            {**base, "dimensions": ["query"], "row_limit": 500},
        )
        g3 = await call(
            "G3", "gsc", "search_analytics.read", {**base, "dimensions": ["page"], "row_limit": 300}
        )
    else:
        skipped.append("G2 and G3, Search Console dates unknown")

    # ---------- compute ----------
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

    channels = {}
    if p1 and not p1["partial"]:
        for row in p1["rows"]:
            name, host = label(row.get("medium"), row.get("ref"), row.get("src"), own)
            period = row.get("period")
            cell = channels.setdefault(
                name, {"current": [0, 0, 0], "prior": [0, 0, 0], "hosts": {}}
            )
            if period in ("current", "prior"):
                counts = (int(row["sessions"]), int(row["visitors"]), int(row["signups"]))
                for i, value in enumerate(counts):
                    cell[period][i] += value
                if host:
                    by_host = cell["hosts"].setdefault(host, {"current": [0, 0], "prior": [0, 0]})
                    by_host[period][0] += counts[0]
                    by_host[period][1] += counts[2]
    elif p1:
        notes.append("P1 channels were truncated at 1,000 rows; channel figures are unknown.")
    if "Referral" in channels:
        hosts = sorted(channels["Referral"]["hosts"].items(), key=lambda kv: -kv[1]["current"][0])
        channels["Referral"]["named"] = [h for h, _ in hosts[:8]]

    signups = {"current": 0, "prior": 0}
    first_touch = {"current": {}, "prior": {}, "d28": {}}
    last_touch, answers = {}, {}
    signups_known = p2 is not None and not p2["partial"]
    if signups_known:
        for row in p2["rows"]:
            period = row.get("period")
            name, _ = label(row.get("fmed"), row.get("fref"), row.get("fsrc"), own)
            counts = (int(row["signups"]), int(row["activated"]), int(row["paid"]))
            for tag in ([period] if period in ("current", "prior") else []) + ["d28"]:
                cell = first_touch[tag].setdefault(name, [0, 0, 0])
                for i, value in enumerate(counts):
                    cell[i] += value
            if period in signups:
                signups[period] += counts[0]
            last, _ = label("", row.get("lref"), "", own)
            last_touch[last] = last_touch.get(last, 0) + counts[0]
            answer = (row.get("answer") or "").strip()
            if answer:
                answers[answer] = answers.get(answer, 0) + counts[0]
    elif p2:
        notes.append("P2 signups were truncated; signup figures are unknown.")
    ranking = "activated signups" if activation else "any signup (activation is not set)"

    content = {}
    if p3 and not p3["partial"]:
        for row in p3["rows"]:
            content.setdefault(row["pth"], {})[row["period"]] = row
    elif p3:
        notes.append("P3 content rows were truncated; content figures are partial.")

    landing = {}
    if p4 and not p4["partial"]:
        for row in p4["rows"]:
            landing.setdefault(row.get("path") or "/", {})[row["period"]] = [
                int(row["sessions"]),
                int(row["signups"]),
            ]

    def gsum(first, end):
        rows = [days[d] for d in days if first <= d <= end]
        clicks = sum(int(r.get("clicks") or 0) for r in rows)
        impressions = sum(int(r.get("impressions") or 0) for r in rows)
        weighted = sum(float(r.get("position") or 0) * int(r.get("impressions") or 0) for r in rows)
        return {
            "clicks": clicks,
            "impressions": impressions,
            "ctr": clicks / impressions if impressions else None,
            "position": weighted / impressions if impressions else None,
            "days": len(rows),
        }

    search = {}
    if weeks_gsc:
        search["current"], search["prior"] = gsum(*weeks_gsc[0]), gsum(*weeks_gsc[1])
        search["weeks"] = [
            gsum(gsc_end - dt.timedelta(days=7 * k + 6), gsc_end - dt.timedelta(days=7 * k))
            | {"end": iso(gsc_end - dt.timedelta(days=7 * k))}
            for k in range(6)
        ]
        months = {}
        for day, row in days.items():
            month = months.setdefault(
                day.strftime("%Y-%m"), {"clicks": 0, "impressions": 0, "days": 0, "reliable": True}
            )
            month["clicks"] += int(row.get("clicks") or 0)
            month["impressions"] += int(row.get("impressions") or 0)
            month["days"] += 1
            if reliable_from and day < reliable_from:
                month["reliable"] = False
        search["months"] = months
    queries = [
        [(r.get("keys") or [""])[0], int(r.get("clicks") or 0), int(r.get("impressions") or 0)]
        for r in (g2 or {}).get("rows", [])
    ]

    def branded(text):
        for term in brand:
            try:
                if re.search(term, text, re.IGNORECASE):
                    return True
            except re.error:
                if term.lower() in text.lower():
                    return True
        return False

    brand_clicks = sum(c for q, c, _ in queries if branded(q)) if brand else None
    read_clicks = sum(c for _, c, _ in queries)
    hidden = (search.get("current", {}).get("clicks", 0) - read_clicks) if g2 else None
    pages = [
        [(r.get("keys") or [""])[0], int(r.get("clicks") or 0), int(r.get("impressions") or 0)]
        for r in (g3 or {}).get("rows", [])
    ]

    t_total = test(
        "total signups",
        signups["current"] if signups_known else None,
        signups["prior"] if signups_known else None,
    )
    t_sessions = test(
        "total sessions",
        sum(c["current"][0] for c in channels.values()) if channels else None,
        sum(c["prior"][0] for c in channels.values()) if channels else None,
    )
    channel_tests = {}
    for name, cell in channels.items():
        if name == "Internal":
            continue
        channel_tests[name] = (
            test(f"{name} sessions", cell["current"][0], cell["prior"][0]),
            test(f"{name} signups", cell["current"][2], cell["prior"][2]),
        )
    t_clicks = None
    if weeks_gsc:
        t_clicks = test("search clicks", search["current"]["clicks"], search["prior"]["clicks"])
    t_nonbrand = None
    if g2 and brand and stored_prev.get("queries") is not None:
        before = (stored_prev.get("totals") or {}).get("nonbrand_clicks")
        t_nonbrand = test("non-brand clicks", read_clicks - (brand_clicks or 0), before)
    for row, adjusted in zip(tests, holm([t["p"] for t in tests]) if tests else [], strict=True):
        row["adj"] = adjusted
        if adjusted < 0.05:
            row["call"] = "change (up)" if row["current"] > row["prior"] else "change (down)"
        else:
            row["call"] = "within normal variation"

    history = stored_prev.get("history") if isinstance(stored_prev.get("history"), dict) else {}
    series_now = {
        "signups": signups["current"] if signups_known else None,
        "sessions": t_sessions.get("current"),
        "search_clicks": search.get("current", {}).get("clicks") if weeks_gsc else None,
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
    if signups_known and signups["current"] == 0 and (t_sessions.get("current") or 0) > 0:
        decisions.append(
            (
                "Check signup tracking",
                f"0 signups this week while {t_sessions['current']} sessions came in "
                f"(PostHog, {span(current_start, today)}).",
                "coding agent: confirm the signup event still fires",
            )
        )
    if signups_known and first_touch["d28"]:
        total = sum(v[0] for v in first_touch["d28"].values())
        direct = first_touch["d28"].get("Direct", [0])[0]
        if total and direct / total >= 0.5 and not answers:
            decisions.append(
                (
                    'Ask new signups "How did you hear about us?"',
                    f"Direct carries {direct} of {total} first-touch signups over 28 days and no "
                    "signup_source answers were recorded.",
                    "growth.signup_source",
                )
            )
    ai_sessions = sum(c["current"][0] for name, c in channels.items() if is_ai(name))
    audit_text = read_text(ctx, AI_AUDIT, notes, quiet=True)
    audit_date = None
    if audit_text:
        found = re.search(r"(20\d\d-\d\d-\d\d)", audit_text[:3000])
        audit_date = dt.date.fromisoformat(found.group(1)) if found else None
    if ai_sessions > 0 and (
        audit_text is None or (audit_date is not None and (today - audit_date).days > 28)
    ):
        age = (
            f"{(today - audit_date).days} days old ({audit_date})." if audit_date else "not found."
        )
        decisions.append(
            (
                "Measure AI visibility again",
                f"{ai_sessions} sessions came from AI assistants this week; the last visibility "
                f"audit is {age}",
                "organic.audit (its buyer questions) or visibility.audit",
            )
        )
    before_calls = stored_prev.get("calls") or {}
    if (
        t_nonbrand
        and t_nonbrand.get("call", "").startswith("change")
        and str(before_calls.get("non-brand clicks", "")).startswith("change")
    ):
        decisions.append(
            (
                "Look at the pages behind the non-brand change",
                f"Non-brand clicks read {t_nonbrand['call']} two weeks running "
                f"({t_nonbrand['current']} vs {t_nonbrand['prior']}).",
                "organic.content_efficacy for page decisions, or organic.audit when many move",
            )
        )
    page_weeks = (
        stored_prev.get("page_weeks") if isinstance(stored_prev.get("page_weeks"), dict) else {}
    )
    for path, clicks, _ in pages:
        past = page_weeks.get(path, [])
        average = sum(past[-4:]) / 4 if len(past) >= 4 else None
        if average is not None and sum(past[-4:]) >= 20 and clicks <= 0.8 * average:
            decisions.append(
                (
                    "Refresh a falling page",
                    f"{path}: {clicks} clicks this week against a prior weekly average of "
                    f"{average:.1f}.",
                    "content.refresh",
                )
            )
    for name, (_sessions_test, signups_test) in channel_tests.items():
        signs = []
        if signups_test.get("call") == "within normal variation" and (
            str(before_calls.get(f"{name} signups", "")) == "within normal variation"
        ):
            signs.append("flat weekly signups")
        if activation and signups_known:
            now_cell = first_touch["current"].get(name, [0, 0, 0])
            before_cell = first_touch["prior"].get(name, [0, 0, 0])
            if (
                before_cell[1] >= min_count
                and now_cell[1] < before_cell[1]
                and signups_test.get("call") in ("within normal variation", "change (up)")
            ):
                p = binomial_p(now_cell[1], before_cell[1])
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
    if "platform_dependence" in optional and signups_known and first_touch["d28"]:
        total = sum(v[0] for v in first_touch["d28"].values())
        top = max(first_touch["d28"].items(), key=lambda kv: kv[1][0])
        if total and top[1][0] / total >= 0.5:
            decisions.append(
                (
                    "Reduce dependence on one channel",
                    f"{top[0]} carries {top[1][0]} of {total} first-touch signups over 28 days.",
                    "founder",
                )
            )
    if "plateau" in optional and stored_prev.get("plateau_weeks", 0) >= 7:
        if not any(t.get("call", "").startswith("change") for t in tests):
            decisions.append(
                (
                    "Call the plateau",
                    "Eight stored weeks show no called change in total signups or channels.",
                    "founder",
                )
            )

    # ---------- write ----------
    lines = []
    called = sorted(
        [t for t in tests if t.get("call", "").startswith("change")], key=lambda t: t["adj"]
    )[:2]
    head = []
    if signups_known:
        head.append(
            f"{signups['current']} people signed up this week against {signups['prior']} last "
            f"week (PostHog, {span(current_start, today)} vs {span(prior_start, current_start)}; "
            f"n = {signups['current'] + signups['prior']}; {t_total.get('call')})."
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
        which = "this week" if launch_now else "last week"
        dates = ", ".join(d.isoformat() for d in (launch_now or launch_prior))
        head.append(
            f"A launch happened {which} ({dates}), so the week-over-week calls compare against a "
            "launch week and are not a trend."
        )
    title = f"{called[0]['key'].capitalize()} moved this week" if called else "Growth this week"
    lines += [f"# {title}", "", " ".join(head), ""]

    lines += ["## Needs a decision"]
    lines += [f"- **{d[0]}.** Evidence: {d[1]} Owner: {d[2]}." for d in decisions] or [
        "Nothing needs a decision this week."
    ]
    lines.append("")

    lines += [
        "## Where people came from",
        f"Ranked by {ranking}. Sessions and signups from PostHog, {span(current_start, today)} vs "
        f"{span(prior_start, current_start)}; first-touch signups by the person's first referrer.",
    ]
    if channels:
        lines += [
            "| Channel | Sessions | Visitors | Signup sessions | First-touch signups | Activated "
            "| Call (sessions) |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]

        def rank_key(item):
            cell = first_touch["current"].get(item[0], [0, 0, 0])
            return (-(cell[1] if activation else cell[0]), -item[1]["current"][0])

        for name, cell in sorted(channels.items(), key=rank_key):
            touch = first_touch["current"].get(name, [0, 0, 0]) if signups_known else None
            if name == "Internal":
                verdict = "not tested (internal)"
            else:
                verdict = channel_tests.get(name, ({}, {}))[0].get("call", "not tested")
            lines.append(
                f"| {name} | {cell['current'][0]} vs {cell['prior'][0]} | {cell['current'][1]} | "
                f"{cell['current'][2]} vs {cell['prior'][2]} | "
                f"{touch[0] if touch else 'unknown'} | "
                f"{touch[1] if touch and activation else 'n/a'} | {verdict} |"
            )
        if "Referral" in channels:
            named = ", ".join(
                f"{h} {channels['Referral']['hosts'][h]['current'][0]}"
                for h in channels["Referral"].get("named", [])
            )
            lines.append(f"Named referrers: {named or 'none'}.")
        direct = channels.get("Direct", {}).get("current", [0])[0]
        lines.append(
            f"Direct is a measurement gap, not a channel: {direct} sessions this week had no "
            "referrer."
        )
    else:
        lines.append(
            "Channel rows are unknown this week" + (" (the read was truncated)." if p1 else ".")
        )
    lines.append("")

    lines += ["## Search"]
    if weeks_gsc:
        now_week, prior_week = search["current"], search["prior"]
        lines.append(
            f"Clicks {span(weeks_gsc[0][0], weeks_gsc[0][1] + dt.timedelta(days=1))} "
            f"(Search Console, final data): {now_week['clicks']} against {prior_week['clicks']} "
            f"the week before; {t_clicks.get('call')}."
        )
        if g2:
            split = (
                f"Brand terms: {brand_clicks} clicks; other queries: {read_clicks - brand_clicks}."
                if brand
                else "brand split skipped: no brand terms"
            )
            lines.append(
                f"Rows read: {read_clicks} clicks; hidden (anonymized queries and rows past the "
                f"cap): {hidden}. {split}"
            )
        if now_week["impressions"] and prior_week["impressions"]:
            lines.append(
                f"Impressions {now_week['impressions']} vs {prior_week['impressions']}; CTR "
                f"{pct(now_week['clicks'], now_week['impressions'])} vs "
                f"{pct(prior_week['clicks'], prior_week['impressions'])}; position "
                f"{now_week['position']:.1f} vs {prior_week['position']:.1f}."
            )
        if (
            t_clicks.get("call") == "change (down)"
            and now_week["impressions"] >= prior_week["impressions"] * 0.8
        ):
            lines.append(
                "Falling CTR with steady impressions may be AI Overviews, not a ranking loss."
            )
        lines.append(
            "Six weeks of clicks (newest first): "
            + ", ".join(str(w["clicks"]) for w in search["weeks"])
            + "."
        )
        by_month = sorted(search["months"].items())[-6:]
        lines.append(
            "By month: "
            + ", ".join(
                f"{k} {v['clicks']} clicks"
                + ("" if v["reliable"] else f" (impressions unreliable before {reliable_from})")
                for k, v in by_month
            )
            + "."
        )
        if stored_prev.get("pages"):
            earlier = {x[0] for x in stored_prev["pages"]}
            new_pages = [pp for pp in pages if pp[2] > 0 and pp[0] not in earlier]
            if new_pages:
                lines.append(
                    "Pages with first impressions this week (approximate): "
                    + ", ".join(pp[0] for pp in new_pages[:10])
                    + "."
                )
    else:
        lines.append("Search Console dates were not read; search figures are unknown.")
    lines.append("")

    lines += ["## AI assistants"]
    ai_rows = [(name, cell) for name, cell in channels.items() if is_ai(name)]
    if ai_rows:
        for name, cell in ai_rows:
            touch = first_touch["current"].get(name, [0, 0, 0])
            lines.append(
                f"- {name}: {cell['current'][0]} sessions vs {cell['prior'][0]}; "
                f"{touch[0]} first-touch signups this week."
            )
    else:
        lines.append(
            "No AI assistant sessions were recorded this week"
            + ("." if channels else " (channels unknown).")
        )
    ai_answers = sum(
        n
        for a, n in answers.items()
        if re.search(r"chatgpt|claude|perplexity|gemini|copilot|\bai\b", a)
    )
    lines.append(
        "Signup answers naming an AI assistant over 28 days: "
        f"{ai_answers if answers else 'no answers recorded'}."
    )
    lines.append(
        f"Visibility audit: {audit_date or ('found, undated' if audit_text else 'no audit found')}."
    )
    lines.append(
        "What stays invisible: app visits without a referrer land in Direct, and AI Overviews and "
        "AI Mode sit inside Search Console's web totals. Branded clicks are the proxy to watch."
    )
    lines.append("")

    lines += [
        "## Content",
        f"Prefixes: {', '.join(content_paths)}. PostHog, {span(current_start, today)} vs "
        f"{span(prior_start, current_start)}.",
    ]
    snap_pages = {}
    for item in ((snap or {}).get("pages") or []) if snap_fresh else []:
        text = str(item.get("page", ""))
        path = "/" + text.split("/", 1)[1] if "/" in text else "/"
        snap_pages[path.rstrip("/") or "/"] = item
    if content:
        top_content = sorted(
            content.items(), key=lambda kv: -int((kv[1].get("current") or {}).get("views") or 0)
        )[:15]
        lines += [
            "| Page | Views | Readers | Median time | >15 s | Read to 90% | "
            "First-touch signups (28 d) |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for path, periods in top_content:
            row = periods.get("current") or {}
            before = periods.get("prior") or {}
            with_duration, with_depth = int(row.get("with_dur") or 0), int(row.get("with_dep") or 0)
            snap_page = snap_pages.get(path.rstrip("/") or "/")
            touches = (
                (snap_page.get("signups") or {}).get("first_touch", [None])[0]
                if snap_page
                else None
            )
            if row.get("median_s") is not None and with_duration >= 5:
                median = f"{round(float(row['median_s']))} s"
            else:
                median = f"too few ({with_duration})" if with_duration else "not tracked"

            def share(part, whole):
                if whole >= 5:
                    return pct(part, whole)
                return "too few" if whole else "not tracked"

            lines.append(
                f"| {path} | {int(row.get('views') or 0)} vs {int(before.get('views') or 0)} | "
                f"{int(row.get('readers') or 0)} | {median} | "
                f"{share(int(row.get('over15') or 0), with_duration)} | "
                f"{share(int(row.get('deep') or 0), with_depth)} | "
                f"{touches if touches is not None else 'unknown'} |"
            )
        lines.append(
            "Assisted signups (people who read a post before signing up) are not measured yet; "
            "first-touch counts come from the traffic snapshot."
        )
    else:
        lines.append(
            "No views under the content prefixes this week" + ("." if p3 else " (not read).")
        )
    lines.append("")

    lines += ["## Landing pages"]
    if landing:
        top_landing = sorted(landing.items(), key=lambda kv: -(kv[1].get("current", [0, 0])[0]))
        lines += [
            "| Entry page | Sessions | Signup rate this week | Last week |",
            "|---|---:|---|---|",
        ]
        for path, value in top_landing[:20]:
            now_cell, before_cell = value.get("current", [0, 0]), value.get("prior", [0, 0])
            lines.append(
                f"| {path} | {now_cell[0]} vs {before_cell[0]} | {now_cell[1]} of {now_cell[0]} "
                f"({pct(now_cell[1], now_cell[0])}) | {before_cell[1]} of {before_cell[0]} "
                f"({pct(before_cell[1], before_cell[0])}) |"
            )
    else:
        lines.append("Landing pages are unknown this week.")
    lines.append("")

    lines += ["## Social, community and email"]
    for name in ("Social", "Email"):
        if name in channels:
            lines.append(
                f"- {name}: {channels[name]['current'][0]} sessions vs "
                f"{channels[name]['prior'][0]}; {channels[name]['current'][2]} signup sessions."
            )
    communities = {
        host: value
        for cell in channels.values()
        for host, value in cell["hosts"].items()
        if community(host)
    }
    for host, value in sorted(communities.items(), key=lambda kv: -kv[1]["current"][0]):
        lines.append(
            f"- {host} (community): {value['current'][0]} sessions vs {value['prior'][0]}; "
            f"{value['current'][1]} signup sessions."
        )
    lines.append(
        "Untagged email clicks and shares in Slack, Discord or WhatsApp arrive as Direct, so a "
        "send or posting week can explain a Direct bump. Open rates are left out because Apple "
        "Mail Privacy Protection preloads the pixel."
    )
    lines.append("")

    lines += ["## Paid"]
    if "Paid" in channels:
        lines.append(
            f"Site-side paid sessions {channels['Paid']['current'][0]} vs "
            f"{channels['Paid']['prior'][0]}; signup sessions {channels['Paid']['current'][2]} "
            "(PostHog)."
        )
    else:
        lines.append("No paid sessions this week (PostHog).")
    lines.append(
        "Ad platform conversions are never added to site-side counts; ads.monitor reports them."
    )
    lines.append("")

    lines += ["## Funnels and attribution (28 days)"]
    if signups_known and first_touch["d28"]:
        lines += ["| First-touch channel | Signups | Activated | Paid |", "|---|---:|---:|---:|"]
        for name, value in sorted(first_touch["d28"].items(), key=lambda kv: -kv[1][0]):
            lines.append(
                f"| {name} | {value[0]} | {value[1] if activation else 'n/a'} | "
                f"{value[2] if paid_event else 'n/a'} |"
            )
        lines.append(
            "Last touch (the signup session's referrer): "
            + ", ".join(f"{k} {v}" for k, v in sorted(last_touch.items(), key=lambda kv: -kv[1]))
            + "."
        )
        top_answers = sorted(answers.items(), key=lambda kv: -kv[1])[:8]
        lines.append(
            "Signup answers: "
            + (", ".join(f"{a} {n}" for a, n in top_answers) if answers else "none recorded")
            + ". Self-reported answers break ties and never make a total."
        )
    else:
        lines.append(
            "Unknown: "
            + (
                "no signup event is set."
                if not signup
                else "the signup read failed or was truncated."
            )
        )
    lines.append("")

    windows_note = (
        f"Windows: PostHog {iso(current_start)} to {iso(today - dt.timedelta(days=1))} vs "
        f"{iso(prior_start)} to {iso(current_start - dt.timedelta(days=1))} (UTC); "
    )
    if weeks_gsc:
        windows_note += (
            f"Search Console {iso(weeks_gsc[0][0])} to {iso(weeks_gsc[0][1])} vs "
            f"{iso(weeks_gsc[1][0])} to {iso(weeks_gsc[1][1])} (Pacific, final data)."
        )
    else:
        windows_note += "Search Console unknown."
    lines += [
        "## Data notes",
        windows_note,
        f"Calls that ran: {', '.join(calls) or 'none'}; no model call. Rows tested: m = "
        f"{len(tests)} (Holm correction).",
        f"Exclusions: {'; '.join(ex_words)}.",
        f"Traffic snapshot: {snap_date or 'not found'}"
        + ("" if snap_fresh or not snap else " (stale)")
        + ".",
        f"Signup event: {signup or 'not set'}; activation: {activation or 'not set'} within "
        f"{window_hours} h; paid event: {paid_event or 'not set'}.",
        (
            "Spike weeks: launch dates "
            + ", ".join(d.isoformat() for d in launches)
            + "; weeks holding one are marked."
            if launches
            else "Spike weeks: no launch_dates input, so no week is marked a windfall."
        ),
    ]
    lines += [f"- skipped: {s}" for s in skipped]
    lines += [f"- {n}" for n in notes + process_notes]
    lines.append("")

    same_week = ((stored_prev.get("windows") or {}).get("posthog") or [None])[0] == iso(
        current_start
    )

    def push(name, value):
        points = list(history.get(name, []))
        if points and points[-1][0] == iso(current_start):
            points = points[:-1]  # a rerun in the same week replaces that week's point
        if value is not None:
            points.append([iso(current_start), value])
        return points[-26:]

    new_page_weeks = {}
    for path, clicks, _ in pages[:100]:
        old = list(page_weeks.get(path, []))
        new_page_weeks[path] = ((old[:-1] if same_week and old else old) + [clicks])[-4:]
    every_test = [t_total, t_sessions] + [x for pair in channel_tests.values() for x in pair]
    every_test += [t for t in (t_clicks, t_nonbrand) if t]
    stored = {
        "schema": "tin.growth_analytics/1",
        "windows": {
            "posthog": [iso(current_start), iso(today)],
            "search": [iso(weeks_gsc[0][0]), iso(weeks_gsc[0][1])] if weeks_gsc else None,
        },
        "spike": bool(launch_now),
        "totals": {
            "signups": signups if signups_known else None,
            "search_clicks": search.get("current", {}).get("clicks") if weeks_gsc else None,
            "brand_clicks": brand_clicks,
            "nonbrand_clicks": (read_clicks - brand_clicks) if (g2 and brand) else None,
            "hidden_clicks": hidden,
        },
        "channels": {
            name: {"sessions": cell["current"][0], "signups": cell["current"][2]}
            for name, cell in channels.items()
        },
        "calls": {t["key"]: t.get("call") for t in every_test},
        "queries": sorted(queries, key=lambda q: -q[1])[:100],
        "pages": sorted(pages, key=lambda p: -p[1])[:100],
        "history": {k: push(k, v) for k, v in series_now.items()},
        "page_weeks": new_page_weeks,
        "plateau_weeks": 0
        if any(t.get("call", "").startswith("change") for t in tests)
        else stored_prev.get("plateau_weeks", 0) + 1,
    }

    def render(numbers):
        body = json.dumps(numbers, separators=(",", ":"), ensure_ascii=False, default=str)
        return "\n".join(lines) + "\n## Stored numbers\n```json\n" + body + "\n```\n"

    report = render(stored)
    for cut in (50, 25, 0):
        if len(report.encode()) <= 63000:
            break
        stored["queries"], stored["pages"] = stored["queries"][:cut], stored["pages"][:cut]
        stored["page_weeks"] = dict(list(stored["page_weeks"].items())[:cut])
        report = render(stored)
    if not calls:
        raise RuntimeError("No read succeeded; last week's report stays in place.")
    return {"path": OUT, "content": report}
