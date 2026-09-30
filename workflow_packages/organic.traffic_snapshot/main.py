"""Weekly traffic snapshot and growth readout: Search Console and PostHog per landing page.

One code workflow writes one file, analytics/traffic-snapshot.json. Its data (pages, totals,
windows, definitions) is what organic.content_efficacy, organic.site_architecture and
content.blog_index read instead of querying the same providers again. Its `readout` holds the
weekly growth readout in Markdown for the founder (readout.py), computed from the same reads:
a code workflow writes exactly one artifact, so the readout travels inside the data file.

No model. Every number is a bounded aggregate read; a failed or partial read leaves its fields
null and names the step in `status_reasons`, never a zero. At most four Search Console and four
PostHog reads, each read serving both the per-page data and the readout.

Dates follow Search Console, which reports days in Pacific time: the 28-day windows and the two
readout weeks end on Search Console's last final day, and the PostHog reads use the same dates.
"""

import datetime as dt
import json
import re
import time
from urllib.parse import urlsplit

from channels import ASSISTANTS, CHANNELS, EXTRA, domain, hogql, quote
from readout import CONTENT, build

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

OUT = "analytics/traffic-snapshot.json"
SCHEMA = "tin.traffic_snapshot/1"
READOUT_SCHEMA = "tin.growth_readout/1"
KEYS = list(CHANNELS + EXTRA)
SHORT = [
    "page",
    "clicks",
    "clicks_prior",
    "impressions",
    "impressions_prior",
    "position",
    "position_prior",
    "sessions",
    "sessions_prior",
    "by_channel",
    "signups",
    "activated",
    "top_query",
    "top_query_impressions",
    "top_query_position",
    "first_seen",
]
TIMEZONE = "America/Los_Angeles"
DEADLINE_SECONDS = 40
ENGAGED = 0.5
# Signup-source answers that name an AI assistant, matched in PostHog.
AI_ANSWER = r"chatgpt|claude|perplexity|gemini|copilot|\bai\b"
# The LIMIT of each PostHog read: a full page of rows means the read may be cut.
CAPS = {"P1": 400, "P2": 300, "P3": 400, "P4": 400}
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
EVENT = re.compile(r"[A-Za-z0-9_$:. -]{1,128}")
PROPERTY = re.compile(r"(?:person:)?[A-Za-z_$][A-Za-z0-9_$]{0,63}")
DOMAIN = re.compile(r"@?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def page_key(url):
    """host/path without a trailing slash: one page whatever the scheme or slash."""
    if not url:
        return None
    text = str(url)
    parts = urlsplit(text if "://" in text else "https://" + text)
    if not parts.hostname:
        return None
    return parts.hostname.lower() + (parts.path.rstrip("/") or "/")


def metric(rows):
    """[clicks, impressions, ctr, impression-weighted position] over Search Console rows."""
    clicks = sum(int(r.get("clicks") or 0) for r in rows)
    impressions = sum(int(r.get("impressions") or 0) for r in rows)
    shown = [r for r in rows if int(r.get("impressions") or 0) > 0]
    position = None
    if impressions and all(number(r.get("position")) is not None for r in shown):
        weighted = sum(number(r["position"]) * int(r["impressions"]) for r in shown)
        position = round(weighted / impressions, 2)
    return [clicks, impressions, round(clicks / impressions, 5) if impressions else None, position]


def provider_rows(response):
    rows = response.get("rows")
    if not isinstance(rows, list):
        raise ValueError("provider response has no rows")
    if rows and isinstance(rows[0], list):
        columns = response.get("columns")
        if not isinstance(columns, list):
            raise ValueError("provider response has no columns")
        return [dict(zip(columns, row, strict=False)) for row in rows]
    return rows


def pacific_today(now):
    try:
        return now.astimezone(ZoneInfo(TIMEZONE)).date()
    except Exception:  # no tz database in the sandbox: UTC-8 is at most an hour off
        return (now - dt.timedelta(hours=8)).date()


def windows(last, assumed):
    """Two 28-day windows, and inside the current one the readout's two seven-day weeks."""
    return {
        "last_final_date": str(last),
        "current": [str(last - dt.timedelta(days=27)), str(last)],
        "prior": [str(last - dt.timedelta(days=55)), str(last - dt.timedelta(days=28))],
        "weeks": {
            "current": [str(last - dt.timedelta(days=6)), str(last)],
            "prior": [str(last - dt.timedelta(days=13)), str(last - dt.timedelta(days=7))],
        },
        "timezone": TIMEZONE,
        "dates": "assumed" if assumed else "search_console",
    }


def reference(prop):
    """A property reference; the name is always a quoted literal."""
    if prop.startswith("person:"):
        return "person.properties[" + quote(prop[7:]) + "]"
    return "properties[" + quote(prop) + "]"


def exclusions(inputs, reasons):
    """Team exclusions from the inputs, as rules and one HogQL condition."""
    domains = inputs.get("exclude_email_domains") or []
    clean = [
        str(d).strip().lower().lstrip("@") for d in domains if DOMAIN.fullmatch(str(d).strip())
    ][:10]
    if len(clean) < len(domains):
        reasons.append("step 2: a malformed email domain was listed and not applied.")
    flag = str(inputs.get("internal_flag_property") or "").strip()
    if flag and not PROPERTY.fullmatch(flag):
        reasons.append("step 2: the internal flag property is not a property name; not applied.")
        flag = ""
    rules, clauses = [], []
    if clean:
        rules.append({"property": "person:email", "op": "suffix", "value": clean})
        # One regular expression for every domain: an address at the domain or a subdomain.
        # HogQL queries are capped at 8000 bytes, so each domain appears once.
        email = "lowerUTF8(ifNull(toString(person.properties.email), ''))"
        pattern = "[@.](" + "|".join(re.escape(d) for d in clean) + ")$"
        clauses.append(f"NOT match({email}, {quote(pattern)})")
    if flag:
        rules.append({"property": flag, "op": "truthy"})
        column = f"lowerUTF8(ifNull(toString({reference(flag)}), ''))"
        clauses.append(f"NOT ({column} IN ('true', '1'))")
    if not rules:
        reasons.append(
            "step 2: no team email domains or internal flag were given, so team visits are counted."
        )
    return rules, " AND ".join(clauses) or "1 = 1"


CURRENT = "('w0', 'w1', 'c')"


def queries(windows_, hosts, excluded, signup, activation, paid, days):
    """The four PostHog reads, each serving the snapshot and the readout.

    P1 landing sessions by page and channel, P2 the week's referring hosts, P3 signups by
    first touch, P4 reading. Every row falls in a bucket: w0 (this week), w1 (the week
    before), c (the rest of the current 28 days) or p (the prior 28 days). HogQL queries are
    capped at 8000 bytes, so raw properties get short names in an inner query first.
    """
    current, prior, weeks = windows_["current"], windows_["prior"], windows_["weeks"]

    def day(column):
        return f"toDate(toTimeZone({column}, '{TIMEZONE}'))"

    def bucket(on="d"):
        return (
            f"multiIf({on} BETWEEN toDate('{weeks['current'][0]}') AND "
            f"toDate('{weeks['current'][1]}'), 'w0', "
            f"{on} BETWEEN toDate('{weeks['prior'][0]}') AND toDate('{weeks['prior'][1]}'), 'w1', "
            f"{on} BETWEEN toDate('{current[0]}') AND toDate('{current[1]}'), 'c', "
            f"{on} BETWEEN toDate('{prior[0]}') AND toDate('{prior[1]}'), 'p', '')"
        )

    scope = "(" + " OR ".join(f"lowerUTF8(toString(properties.$host)) = {quote(h)}" for h in hosts)
    scope += ")"
    span = f"{day('timestamp')} BETWEEN toDate('{prior[0]}') AND toDate('{current[1]}')"
    channel = hogql("um", "rd", "us", hosts)
    signed = f"event = {quote(signup)}" if signup else "0 = 1"

    # One row per PostHog session ($session_id), as PostHog counts sessions. Grouping by
    # (distinct_id, session) counted a session twice when a visitor signed in mid-session and
    # their distinct ID changed. A session belongs to the bucket and landing page of its first
    # pageview. Pageviews without a session ID count as pageviews, never as sessions.
    def sessions(with_host=False):
        return (
            "WITH r AS (SELECT if(ifNull(toString(properties.$session_id), '') = '', "
            "concat('~', toString(distinct_id)), toString(properties.$session_id)) AS session, "
            f"distinct_id AS did, timestamp, event, {day('timestamp')} AS d, "
            "toString(properties.$pathname) AS path, toString(properties.utm_medium) AS um, "
            "toString(properties.$referring_domain) AS rd, toString(properties.utm_source) AS us "
            f"FROM events WHERE ((event = '$pageview' AND {scope}) OR {signed}) AND {excluded} "
            f"AND {span}), v AS (SELECT session, did, timestamp, event, {bucket()} AS w, path, "
            f"{channel} AS channel"
            + (
                ", replaceRegexpOne(lowerUTF8(ifNull(rd, '')), '^www\\\\.', '') AS host"
                if with_host
                else ""
            )
            + " FROM r), "
            "e AS (SELECT session, any(did) AS did, argMinIf(w, timestamp, event = '$pageview') "
            "AS w, argMinIf(path, timestamp, event = '$pageview') AS path, "
            "argMinIf(channel, timestamp, event = '$pageview') AS channel, "
            + ("argMinIf(host, timestamp, event = '$pageview') AS host, " if with_host else "")
            + "countIf(event = '$pageview') AS pageviews, "
            f"countIf({signed}) > 0 AS signed_up, startsWith(session, '~') AS sessionless "
            "FROM v GROUP BY session HAVING pageviews > 0) "
        )

    landing = sessions() + (
        f"SELECT path, channel, countIf(NOT sessionless AND w IN {CURRENT}) AS sessions, "
        "countIf(NOT sessionless AND w = 'p') AS sessions_prior, "
        f"sumIf(pageviews, w IN {CURRENT}) AS pageviews, "
        "sumIf(pageviews, w = 'p') AS pageviews_prior, "
        "countIf(NOT sessionless AND w = 'w0') AS week, "
        "countIf(NOT sessionless AND w = 'w1') AS week_prior, "
        "countIf(NOT sessionless AND w = 'w0' AND signed_up) AS week_signups, "
        "countIf(NOT sessionless AND w = 'w1' AND signed_up) AS week_prior_signups "
        "FROM e WHERE w != '' GROUP BY path, channel "
        "ORDER BY sessions + sessions_prior DESC LIMIT 400"
    )
    referrers = sessions(with_host=True) + (
        "SELECT channel, host, countIf(w = 'w0') AS week, countIf(w = 'w1') AS week_prior, "
        "uniqExactIf(did, w = 'w0') AS visitors, "
        "countIf(w = 'w0' AND signed_up) AS week_signups FROM e "
        "WHERE NOT sessionless AND w IN ('w0', 'w1') "
        "AND channel IN ('AI assistants', 'Referral', 'Social', 'Email') "
        "GROUP BY channel, host ORDER BY week + week_prior DESC LIMIT 300"
    )
    active, bought = quote(activation or "__none__"), quote(paid or "__none__")
    first = quote(signup)
    signups = (
        "WITH r AS (SELECT person_id, timestamp, event, "
        "person.properties.$initial_pathname AS path, "
        "toString(person.properties.$initial_utm_medium) AS um, "
        "toString(person.properties.$initial_referring_domain) AS rd, "
        "toString(person.properties.$initial_utm_source) AS us, "
        "lowerUTF8(trim(ifNull(toString(person.properties.signup_source), "
        "ifNull(toString(person.properties.self_reported_source), '')))) AS answer "
        f"FROM events WHERE event IN ({first}, {active}, {bought}) AND {excluded} "
        "AND timestamp >= now() - INTERVAL 180 DAY), "
        f"x AS (SELECT person_id, timestamp, event, path, answer, {channel} AS channel, "
        "replaceRegexpOne(lowerUTF8(ifNull(rd, '')), '^www\\\\.', '') AS ref FROM r), "
        f"s AS (SELECT person_id, minIf(timestamp, event = {first}) AS signup_at, "
        f"minIf(timestamp, event = {active}) AS active_at, "
        f"minIf(timestamp, event = {bought}) AS paid_at, "
        f"argMinIf(path, timestamp, event = {first}) AS path, "
        f"argMinIf(channel, timestamp, event = {first}) AS channel, "
        f"argMinIf(ref, timestamp, event = {first}) AS ref, any(answer) AS answer "
        "FROM x GROUP BY person_id), "
        f"t AS (SELECT path, channel, ref, answer, {bucket(day('signup_at'))} AS w, "
        f"active_at >= signup_at AND active_at <= signup_at + INTERVAL {days} DAY AS act, "
        f"active_at < signup_at AND now() < signup_at + INTERVAL {days} DAY AS pending, "
        "paid_at >= signup_at AND paid_at <= signup_at + INTERVAL 28 DAY AS bought "
        "FROM s WHERE signup_at > toDateTime('1970-01-01')) "
        "SELECT path, channel, if(channel = 'AI assistants', ref, '') AS assistant, "
        f"countIf(w IN {CURRENT}) AS signups, countIf(w = 'p') AS signups_prior, "
        f"countIf(w IN {CURRENT} AND act) AS activated, countIf(w = 'p' AND act) "
        "AS activated_prior, countIf(pending) AS open, "
        "countIf(w = 'w0') AS week, countIf(w = 'w1') AS week_prior, "
        "countIf(w = 'w0' AND act) AS week_activated, "
        "countIf(w = 'w1' AND act) AS week_prior_activated, "
        f"countIf(w IN {CURRENT} AND bought) AS paid, "
        f"countIf(w IN {CURRENT} AND answer != '') AS answered, "
        f"countIf(w IN {CURRENT} AND match(answer, {quote(AI_ANSWER)})) AS ai_answers "
        "FROM t WHERE w != '' GROUP BY path, channel, assistant "
        "ORDER BY signups + signups_prior DESC LIMIT 400"
    )
    depth = "toFloat64OrNull(toString(properties.$prev_pageview_max_content_percentage))"
    reading = (
        "SELECT path, countIf(event = '$pageview' AND w = 'w0') AS views, "
        "countIf(event = '$pageview' AND w = 'w1') AS views_prior, "
        "uniqExactIf(did, event = '$pageview' AND w = 'w0') AS readers, "
        "countIf(event = '$pageleave') AS reads, "
        "medianIf(dur, event = '$pageleave') AS median_seconds, "
        f"countIf(event = '$pageleave' AND dep >= {ENGAGED}) AS engaged, "
        "countIf(event = '$pageleave' AND dep >= 0.9) AS finished, "
        "countIf(event = '$pageleave' AND w = 'w0' AND dur IS NOT NULL) AS week_timed, "
        "countIf(event = '$pageleave' AND w = 'w0' AND dur > 15) AS week_over15, "
        "countIf(event = '$pageleave' AND w = 'w0' AND dep IS NOT NULL) AS week_depth, "
        "countIf(event = '$pageleave' AND w = 'w0' AND dep >= 0.9) AS week_deep, "
        "medianIf(dur, event = '$pageleave' AND w = 'w0') AS week_median FROM ("
        f"SELECT event, distinct_id AS did, {bucket(day('timestamp'))} AS w, "
        "if(event = '$pageleave', ifNull(toString(properties.$prev_pageview_pathname), "
        "toString(properties.$pathname)), toString(properties.$pathname)) AS path, "
        "toFloat64OrNull(toString(properties.$prev_pageview_duration)) AS dur, "
        f"if({depth} > 1, {depth} / 100, {depth}) AS dep FROM events "
        f"WHERE event IN ('$pageview', '$pageleave') AND {scope} AND {excluded} AND "
        f"{day('timestamp')} BETWEEN toDate('{current[0]}') AND toDate('{current[1]}')) "
        "GROUP BY path ORDER BY reads + views DESC LIMIT 400"
    )
    return {"P1": landing, "P2": referrers, "P3": signups, "P4": reading}


def blank_visits():
    return {"sessions": None, "pageviews": None, "by_channel": None}


def zero_visits():
    return {"sessions": 0, "pageviews": 0, "by_channel": [0] * len(KEYS)}


def blank(key, first_seen):
    return {
        "page": key,
        "rank": None,
        "pinned": False,
        "variants": 1,
        "first_seen": first_seen,
        "first_seen_exact": False,
        "search": {
            "current": None,
            "prior": None,
            "prior_note": None,
            "branded_clicks": None,
            "other_named_clicks": None,
            "hidden_clicks": None,
            "query_rows": None,
            "queries": [],
        },
        "visits": {"current": blank_visits(), "prior": blank_visits()},
        "reading": {"reads": None, "median_seconds": None, "engaged": None, "finished": None},
        "signups": {"first_touch": [None, None], "activated": [None, None], "open": None},
        "audit": [],
        "flags": [],
    }


def short(page):
    """The compact row for pages outside the detailed list; columns are SHORT."""
    search, visits = page["search"], page["visits"]
    top = search["queries"][0] if search["queries"] else [None] * 5
    now, before = search["current"], search["prior"]
    return [
        page["page"],
        now[0] if now else None,
        before[0] if before else None,
        now[1] if now else None,
        before[1] if before else None,
        now[3] if now else None,
        before[3] if before else None,
        visits["current"]["sessions"],
        visits["prior"]["sessions"],
        visits["current"]["by_channel"],
        page["signups"]["first_touch"][0],
        page["signups"]["activated"][0],
        top[0],
        top[2],
        top[3],
        page["first_seen"],
    ]


def attach_audit(ctx, inputs, previous, objects, reasons, today):
    """Pin the organic audit's findings to the pages they name.

    `seen_on` is the day a snapshot first attached this audit run; the readout uses it to
    say how old the audit's AI answers are.
    """
    before = (previous or {}).get("audit") or {}
    known = before.get("known_run_ids") or []
    audit = {
        "status": "none",
        "run_id": None,
        "seen_on": None,
        "target_host": None,
        "known_run_ids": known,
        "coverage_status": None,
        "inspected_pages": None,
        "sitemap_pages": None,
        "pages_with_findings": 0,
        "not_listed": 0,
    }
    try:
        paths = ctx.files.glob("reports/organic-audit/*/findings.json")
    except (ValueError, OSError):
        paths = []
        reasons.append("step 9: organic.audit findings could not be listed.")
    runs = {p.split("/")[2]: p for p in paths if len(p.split("/")) == 4}
    chosen = inputs.get("audit_run_id")
    new = [run for run in runs if run not in known]
    if not chosen:
        if len(new) == 1:
            chosen = new[0]
        elif len(new) > 1:
            audit["status"] = "ambiguous"
            reasons.append(
                "step 9: several new organic.audit runs were found; name one as audit_run_id."
            )
        elif ((previous or {}).get("audit") or {}).get("run_id") in runs:
            chosen = previous["audit"]["run_id"]
    if not chosen:
        return audit
    if chosen not in runs:
        reasons.append("step 9: the selected organic.audit findings were not found.")
        return audit
    try:
        data = json.loads(ctx.files.read_text(runs[chosen]))
    except FileNotFoundError:
        reasons.append("step 9: the selected organic.audit findings were not found.")
        return audit
    except (ValueError, OSError):
        # Larger than a 64 KB read, or not JSON: the snapshot still ships without it.
        audit["status"] = "unreadable"
        reasons.append("step 9: organic.audit findings could not be read (over 64 KB or invalid).")
        return audit
    if not isinstance(data, dict) or data.get("schema_version") != 3:
        audit["status"] = "unreadable"
        reasons.append("step 9: organic.audit findings are not schema 3.")
        return audit
    coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    audit.update(
        status="attached",
        run_id=chosen,
        seen_on=(before.get("seen_on") if before.get("run_id") == chosen else None) or str(today),
        target_host=data.get("target_host"),
        known_run_ids=list(dict.fromkeys(known + [chosen])),
        coverage_status=data.get("coverage_status"),
        inspected_pages=coverage.get("inspected_pages"),
        sitemap_pages=coverage.get("sitemap_pages"),
    )
    found = set()
    for finding in data.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        for url in finding.get("urls") or []:
            key = page_key(url)
            if key in objects:
                objects[key]["audit"].append(
                    [finding.get("id"), finding.get("check_id"), finding.get("priority")]
                )
                found.add(key)
            else:
                audit["not_listed"] += 1
    audit["pages_with_findings"] = len(found)
    return audit


def audit_host(ctx):
    """The newest organic audit's own host, when a findings file is small enough to read."""
    try:
        paths = sorted(ctx.files.glob("reports/organic-audit/*/findings.json"))
    except (ValueError, OSError):
        return None
    for path in reversed(paths[-3:]):
        try:
            data = json.loads(ctx.files.read_text(path))
        except (ValueError, OSError):
            continue
        host = str((data or {}).get("target_host") or "").lower()
        if HOST.fullmatch(host):
            return host
    return None


def event_name(inputs, name):
    value = str(inputs.get(name) or "").strip() or None
    if value and not EVENT.fullmatch(value):
        raise ValueError(f"{name} is not an event name.")
    return value


def readout_inputs(inputs, reasons):
    """The readout's own settings: test threshold, optional rules, launches, content paths."""
    launches = []
    for value in inputs.get("launch_dates") or []:
        try:
            launches.append(dt.date.fromisoformat(str(value)[:10]))
        except ValueError:
            reasons.append(f"readout: launch date {str(value)[:10]} is not a date; ignored.")
    paths = [
        p
        for p in inputs.get("content_paths") or CONTENT
        if isinstance(p, str) and p.startswith("/")
    ][:8]
    return {
        "min_count": int(inputs.get("min_count") or 20),
        "optional_rules": set(inputs.get("optional_rules") or []),
        "launch_dates": launches,
        "content_paths": paths,
    }


def label(channel, host):
    """A readout row label: the channel, with each named AI assistant on its own row."""
    if channel == "AI assistants":
        return ASSISTANTS.get(domain(host), "AI assistants (other)")
    return channel


async def run(ctx, inputs):
    started = time.monotonic()
    now = dt.datetime.now(dt.UTC)
    today = pacific_today(now)
    reasons, calls = [], []
    returned = 0

    try:
        previous = json.loads(ctx.files.read_text(OUT))
    except FileNotFoundError:
        previous = None
    except (ValueError, OSError):
        previous = None
        reasons.append("step 1: the previous snapshot could not be read.")
    if not isinstance(previous, dict):
        previous = None

    signup = event_name(inputs, "signup_event")
    activation = event_name(inputs, "activation_event")
    paid = event_name(inputs, "paid_event")
    days = int(inputs.get("activation_window_days") or 7)
    if not signup:
        reasons.append("step 1: no signup_event was given; signups are not measured.")
    if not activation:
        reasons.append("step 1: no activation_event was given; activation is not measured.")
    rules, excluded = exclusions(inputs, reasons)
    settings = readout_inputs(inputs, reasons)
    reliable_from = None
    if inputs.get("impressions_reliable_from"):
        reliable_from = dt.date.fromisoformat(str(inputs["impressions_reliable_from"])[:10])

    hosts = [str(h).strip().lower() for h in inputs.get("website_hosts") or []]
    bad = [h for h in hosts if not HOST.fullmatch(h)]
    if bad:
        raise ValueError("website_hosts takes host names such as example.com, not URLs.")
    host_source = "inputs" if hosts else None
    brands = [str(b) for b in inputs.get("brand_terms") or []][:20]

    async def call(step, service, operation, arguments):
        nonlocal returned
        if time.monotonic() - started >= DEADLINE_SECONDS:
            calls.append({"step": step, "operation": operation, "outcome": "skipped", "rows": 0})
            reasons.append(f"step {step}: the call deadline was reached.")
            return None
        began = time.monotonic()
        try:
            response = await ctx.services.call(
                service=service, step=step, operation=operation, arguments=arguments
            )
            rows = provider_rows(response)
        except ValueError as exc:
            if str(exc).startswith("The service request differs from its declared contract"):
                raise  # a package bug, not a provider outcome
            refused = any(
                word in str(exc).lower() for word in ("refus", "permission", "too large", "reject")
            )
            calls.append(
                {
                    "step": step,
                    "operation": operation,
                    "outcome": "refused" if refused else "incomplete",
                    "rows": 0,
                    "seconds": round(time.monotonic() - began, 2),
                }
            )
            reasons.append(f"step {step}: {str(exc)[:120]}.")
            return None
        cap = arguments.get("row_limit") if service == "gsc" else CAPS.get(step, 400)
        partial = bool(
            response.get("truncated")
            or response.get("has_more")
            or response.get("next_start_row")
            or (cap and len(rows) >= cap)
        )
        calls.append(
            {
                "step": step,
                "operation": operation,
                "outcome": "truncated" if partial else "ok",
                "rows": len(rows),
                "seconds": round(time.monotonic() - began, 2),
            }
        )
        returned += 1
        if partial:
            reasons.append(f"step {step}: the provider returned a partial page of rows.")
        return rows, partial

    def search(dimensions, span, filters=None, limit=1000):
        return {
            "start_date": span[0],
            "end_date": span[1],
            "dimensions": dimensions,
            "row_limit": limit,
            "start_row": 0,
            "dimension_filters": filters or [],
        }

    # G1: daily totals for a year, which also give Search Console's last final day.
    window = windows(today - dt.timedelta(days=3), True)
    totals = {
        "current": None,
        "prior": None,
        "branded_clicks": None,
        "other_named_clicks": None,
        "hidden_clicks": None,
        "branded_scope": "listed pages",
    }
    weeks, months = [], []
    daily = await call(
        "G1",
        "gsc",
        "search_analytics.read",
        search(["date"], [str(today - dt.timedelta(days=365)), str(today - dt.timedelta(days=1))]),
    )
    dated = []
    if daily:
        for row in daily[0]:
            try:
                dated.append({**row, "date": dt.date.fromisoformat(str(row["keys"][0]))})
            except (KeyError, IndexError, TypeError, ValueError):
                continue
    if dated:
        last = max(row["date"] for row in dated)
        window = windows(last, False)
        for name in ("current", "prior"):
            first, end = window[name]
            rows = [r for r in dated if first <= str(r["date"]) <= end]
            if len(rows) == 28 and not daily[1]:
                totals[name] = metric(rows)
            else:
                reasons.append(
                    f"step G1: the {name} site total is not measured; dates are missing."
                )

        def trusted(day):
            return reliable_from is None or day >= reliable_from

        for index in range(12):
            end = last - dt.timedelta(days=7 * index)
            begin = end - dt.timedelta(days=6)
            rows = [r for r in dated if begin <= r["date"] <= end]
            value = metric(rows) if len(rows) == 7 and not daily[1] else [None] * 4
            ok = trusted(begin)
            weeks.append(
                {
                    "start": str(begin),
                    "end": str(end),
                    "clicks": value[0],
                    "impressions": value[1] if ok else None,
                    "ctr": value[2] if ok else None,
                    "position": value[3] if ok else None,
                    "reliable": ok,
                }
            )
        for month in sorted({str(r["date"])[:7] for r in dated}, reverse=True)[:12]:
            rows = [r for r in dated if str(r["date"]).startswith(month)]
            value = metric(rows)
            ok = trusted(min(r["date"] for r in rows))
            months.append(
                {
                    "month": month,
                    "days": len(rows),
                    "clicks": value[0] if not daily[1] else None,
                    "impressions": value[1] if ok and not daily[1] else None,
                    "reliable": ok,
                }
            )
    else:
        reasons.append("step G1: no final Search Console date was read; the dates are assumed.")

    # G2/G3: pages in the current and prior windows.
    responses = {}
    for step, name in (("G2", "current"), ("G3", "prior")):
        responses[name] = await call(
            step, "gsc", "search_analytics.read", search(["page"], window[name], limit=400)
        )
    maps, variants = {}, {}
    for name in ("current", "prior"):
        grouped, counted = {}, {}
        if responses[name]:
            for row in responses[name][0]:
                key = page_key((row.get("keys") or [None])[0])
                if key:
                    grouped.setdefault(key, []).append(row)
                    counted[key] = counted.get(key, 0) + 1
        maps[name] = {key: metric(rows) for key, rows in grouped.items()}
        variants[name] = counted

    if not hosts:
        found = audit_host(ctx)
        if found:
            hosts, host_source = [found], "organic.audit"
    if not hosts and maps["current"]:
        ranked_hosts = sorted(maps["current"], key=lambda k: maps["current"][k][0], reverse=True)
        hosts = list(dict.fromkeys(k.split("/", 1)[0] for k in ranked_hosts))[:5]
        host_source = "search_console"
    if not hosts:
        reasons.append("step 2: no website host was found; pageview queries cannot be scoped.")
    brand_source = "inputs" if brands else None
    if not brands:
        for host in hosts:
            parts = host.removeprefix("www.").split(".")
            if len(parts) > 1:
                brands.append(re.escape(parts[0]) + r"[ .-]?" + re.escape(parts[1]))
            if len(parts[0]) >= 5:
                brands.append(re.escape(parts[0]))
        brands = brands[:20]
        brand_source = "host" if brands else "none"
    if not brands:
        reasons.append("step 2: no brand term could be derived; branded clicks are not measured.")
    valid = []
    for term in brands:
        try:
            re.compile(term, re.IGNORECASE)
            valid.append(term)
        except re.error:
            reasons.append("step 2: an invalid brand pattern was left out.")
    brands = valid

    pinned = {
        page_key(host + str(path))
        for host in hosts
        for path in inputs.get("always_include") or []
        if str(path).startswith("/")
    }
    pinned.discard(None)
    current = maps["current"]
    ranked = sorted(current, key=lambda k: (current[k][1], current[k][0]), reverse=True)
    detail = list(dict.fromkeys(ranked[:30] + sorted(pinned)))[:50]
    more = [k for k in ranked if k not in detail][:60]
    complete_current = responses["current"] and not responses["current"][1]
    dropped = []
    if complete_current:
        prior_ranked = sorted(maps["prior"], key=lambda k: maps["prior"][k][0], reverse=True)
        dropped = [k for k in prior_ranked if k not in current][:15]

    first_seen = {
        p.get("page"): p.get("first_seen")
        for p in (previous or {}).get("pages") or []
        if isinstance(p, dict)
    }
    for section in ("more_pages", "entry_only_pages", "dropped_pages"):
        for row in (previous or {}).get(section) or []:
            if isinstance(row, list) and len(row) > 15 and row[0] and row[15]:
                first_seen.setdefault(row[0], row[15])
    objects = {}
    for rank, key in enumerate(dict.fromkeys(detail + more + dropped), 1):
        page = blank(key, first_seen.get(key) or window["current"][0])
        page["rank"], page["pinned"] = rank, key in pinned
        page["variants"] = max(variants["current"].get(key, 0), variants["prior"].get(key, 0), 1)
        for name in ("current", "prior"):
            response = responses[name]
            if key in maps[name]:
                page["search"][name] = maps[name][key]
            elif response and not response[1]:
                page["search"][name] = [0, 0, None, None]
            elif name == "prior" and response and response[1]:
                page["search"]["prior_note"] = (
                    f"The prior read ended at {response[0][-1].get('clicks')} clicks; "
                    "this page may be below it."
                    if response[0]
                    else "The prior read was truncated."
                )
        prior_complete = responses["prior"] and not responses["prior"][1]
        if key in current and key not in maps["prior"] and prior_complete:
            page["flags"].append("new_in_search")
        objects[key] = page

    # G4: queries for every listed page with impressions, clicked pages first.
    shown = [k for k in detail if current.get(k, [0, 0])[1] > 0]
    shown.sort(key=lambda k: current[k][0] == 0)
    expression = "|".join(re.escape("https://" + k) for k in shown)
    while shown and len(expression) > 4096:
        shown.pop()
        expression = "|".join(re.escape("https://" + k) for k in shown)
    read = None
    if shown:
        filters = [{"dimension": "page", "operator": "includingRegex", "expression": expression}]
        read = await call(
            "G4",
            "gsc",
            "search_analytics.read",
            search(["page", "query"], window["current"], filters, limit=500),
        )
    else:
        calls.append(
            {"step": "G4", "operation": "search_analytics.read", "outcome": "skipped", "rows": 0}
        )
    if read:
        by_page = {}
        for row in read[0]:
            parts = row.get("keys") or []
            if len(parts) < 2:
                continue
            key, query = page_key(parts[0]), str(parts[1])
            if key in objects:
                named = any(re.search(t, query, re.IGNORECASE) for t in brands if len(t) < 200)
                by_page.setdefault(key, []).append(
                    [
                        query,
                        int(row.get("clicks") or 0),
                        int(row.get("impressions") or 0),
                        number(row.get("position")),
                        named,
                    ]
                )
        for key in shown:
            page = objects[key]
            rows = sorted(by_page.get(key, []), key=lambda q: (q[1], q[2]), reverse=True)
            page["search"]["queries"], page["search"]["query_rows"] = rows[:5], len(rows)
            if not read[1] and page["search"]["current"]:
                page["search"]["branded_clicks"] = (
                    sum(q[1] for q in rows if q[4]) if brands else None
                )
                page["search"]["other_named_clicks"] = sum(q[1] for q in rows if not q[4])
                page["search"]["hidden_clicks"] = max(
                    0, page["search"]["current"][0] - sum(q[1] for q in rows)
                )

    # P1-P4: PostHog, scoped to the site's hosts and the team exclusions.
    posthog = {}
    sql = queries(window, hosts, excluded, signup, activation, paid, days) if hosts else {}
    for step in ("P1", "P2", "P3", "P4"):
        if not hosts or (step == "P3" and not signup):
            calls.append(
                {"step": step, "operation": "query.hogql", "outcome": "skipped", "rows": 0}
            )
            reasons.append(f"step {step}: no scoped query could be sent.")
            posthog[step] = None
            continue
        if len(sql[step].encode()) > 8000:
            raise ValueError(f"{step} query exceeds 8000 bytes")
        posthog[step] = await call(
            step, "posthog", "query.hogql", {"query": sql[step], "name": f"traffic {step}"}
        )

    visits = {"current": blank_visits(), "prior": blank_visits()}
    signup_totals = {"current": None, "prior": None, "by_channel": {"current": None, "prior": None}}
    active_totals = {"current": None, "prior": None, "open": None}
    reading = {"reads": None, "engaged": None, "finished": None}
    landing, landing_week, channel_week = {}, {}, None
    home = hosts[0] if hosts else ""
    if posthog.get("P1"):
        totals_by_window = {name: zero_visits() for name in ("current", "prior")}
        weekly = {}
        for row in posthog["P1"][0]:
            channel = row.get("channel")
            key = page_key(home + str(row.get("path") or "/"))
            if channel not in KEYS or not key:
                continue
            index = KEYS.index(channel)
            for name, sessions, views in (
                ("current", row.get("sessions"), row.get("pageviews")),
                ("prior", row.get("sessions_prior"), row.get("pageviews_prior")),
            ):
                sessions, views = int(sessions or 0), int(views or 0)
                for bucket in (
                    totals_by_window[name],
                    landing.setdefault(key, {z: zero_visits() for z in ("current", "prior")})[name],
                ):
                    bucket["sessions"] += sessions
                    bucket["pageviews"] += views
                    bucket["by_channel"][index] += sessions
            counts = [
                int(row.get(k) or 0)
                for k in ("week", "week_prior", "week_signups", "week_prior_signups")
            ]
            path = "/" + key.split("/", 1)[1] if "/" in key else "/"
            for target, name in ((weekly, channel), (landing_week, path)):
                cell = target.setdefault(name, [0, 0, 0, 0])
                for i, value in enumerate(counts):
                    cell[i] += value
        if not posthog["P1"][1]:
            visits = totals_by_window
            channel_week = {k: v for k, v in weekly.items() if v[0] or v[1]}
            for key, page in objects.items():
                page["visits"] = {z: landing.get(key, {}).get(z, zero_visits()) for z in visits}
        else:
            for key, page in objects.items():
                if key in landing:
                    page["visits"] = landing[key]
            reasons.append("step P1: landing pages below the last row were not measured.")
        for page in objects.values():
            clicks = (page["search"]["current"] or [0])[0]
            if clicks >= 10 and page["visits"]["current"]["sessions"] == 0:
                page["flags"].append("no_visits_with_clicks")

    referrers, assistants = None, None
    if posthog.get("P2") and not posthog["P2"][1]:
        referrers, assistants = [], {}
        for row in posthog["P2"][0]:
            channel, host = row.get("channel"), domain(row.get("host"))
            counts = [int(row.get(k) or 0) for k in ("week", "week_prior", "visitors")]
            signups_week = int(row.get("week_signups") or 0)
            referrers.append((channel, host, counts[0], counts[1], counts[2], signups_week))
            if channel == "AI assistants":
                cell = assistants.setdefault(label(channel, host), [0, 0, 0, 0])
                for i, value in enumerate(counts + [signups_week]):
                    cell[i] += value
        referrers.sort(key=lambda r: -r[2])

    signups_week, touch, answers = None, None, None
    if posthog.get("P3"):
        rows, partial = posthog["P3"]
        if not any(
            (number(r.get("signups")) or 0) + (number(r.get("signups_prior")) or 0) > 0
            for r in rows
        ):
            reasons.append(
                "step P3: the signup event was not seen in the last 56 days; check the name."
            )
        elif partial:
            reasons.append("step P3: the signup read was truncated; totals are not measured.")
        else:
            signup_totals = {
                "current": 0,
                "prior": 0,
                "by_channel": {"current": [0] * len(KEYS), "prior": [0] * len(KEYS)},
            }
            if activation:
                active_totals = {"current": 0, "prior": 0, "open": 0}
            for page in objects.values():
                page["signups"] = {
                    "first_touch": [0, 0],
                    "activated": [0, 0] if activation else [None, None],
                    "open": 0 if activation else None,
                }
            signups_week, touch, answers = [0, 0], {}, {"answered": 0, "ai": 0}
            for row in rows:
                channel = row.get("channel")
                count = {
                    k: int(row.get(k) or 0)
                    for k in (
                        "signups",
                        "signups_prior",
                        "activated",
                        "activated_prior",
                        "open",
                        "week",
                        "week_prior",
                        "week_activated",
                        "week_prior_activated",
                        "paid",
                        "answered",
                        "ai_answers",
                    )
                }
                signup_totals["current"] += count["signups"]
                signup_totals["prior"] += count["signups_prior"]
                if channel in KEYS:
                    signup_totals["by_channel"]["current"][KEYS.index(channel)] += count["signups"]
                    signup_totals["by_channel"]["prior"][KEYS.index(channel)] += count[
                        "signups_prior"
                    ]
                if activation:
                    active_totals["current"] += count["activated"]
                    active_totals["prior"] += count["activated_prior"]
                    active_totals["open"] += count["open"]
                key = page_key(home + str(row.get("path") or "/"))
                if key in objects:
                    page = objects[key]
                    page["signups"]["first_touch"][0] += count["signups"]
                    page["signups"]["first_touch"][1] += count["signups_prior"]
                    if activation:
                        page["signups"]["activated"][0] += count["activated"]
                        page["signups"]["activated"][1] += count["activated_prior"]
                        page["signups"]["open"] += count["open"]
                signups_week[0] += count["week"]
                signups_week[1] += count["week_prior"]
                answers["answered"] += count["answered"]
                answers["ai"] += count["ai_answers"]
                cell = touch.setdefault(
                    label(channel, row.get("assistant")),
                    dict.fromkeys(
                        (
                            "week",
                            "week_prior",
                            "week_activated",
                            "prior_activated",
                            "signups",
                            "activated",
                            "paid",
                        ),
                        0,
                    ),
                )
                cell["week"] += count["week"]
                cell["week_prior"] += count["week_prior"]
                cell["week_activated"] += count["week_activated"]
                cell["prior_activated"] += count["week_prior_activated"]
                cell["signups"] += count["signups"]
                cell["activated"] += count["activated"]
                cell["paid"] += count["paid"]
            touch = {k: v for k, v in touch.items() if any(v.values())}

    content = None
    if posthog.get("P4"):
        rows, partial = posthog["P4"]
        if not rows:
            reasons.append("step P4: no pageview or pageleave events were read.")
        elif partial:
            reasons.append("step P4: reading rows were cut at the read limit; totals stay empty.")
        elif any(int(r.get("reads") or 0) for r in rows):
            reading = {"reads": 0, "engaged": 0, "finished": 0}
        else:
            reasons.append("step P4: no pageleave events were seen; reading is not tracked.")
        content = []
        prefixes = settings["content_paths"]
        for row in rows:
            reads = int(row.get("reads") or 0)
            deep, done = int(row.get("engaged") or 0), int(row.get("finished") or 0)
            path = str(row.get("path") or "/")
            if reading["reads"] is not None:
                reading["reads"] += reads
                reading["engaged"] += deep
                reading["finished"] += done
            key = page_key(home + path)
            if key in objects and reads and reading["reads"] is not None:
                objects[key]["reading"] = {
                    "reads": reads,
                    "median_seconds": number(row.get("median_seconds")),
                    "engaged": deep,
                    "finished": done,
                }
            if any(path.startswith(p) for p in prefixes) and (
                int(row.get("views") or 0) or int(row.get("views_prior") or 0)
            ):
                page = objects.get(key)
                content.append(
                    {
                        "path": path,
                        "views": int(row.get("views") or 0),
                        "views_prior": int(row.get("views_prior") or 0),
                        "readers": int(row.get("readers") or 0),
                        "median": number(row.get("week_median")),
                        "timed": int(row.get("week_timed") or 0),
                        "over15": int(row.get("week_over15") or 0),
                        "depth": int(row.get("week_depth") or 0),
                        "deep": int(row.get("week_deep") or 0),
                        "touches": page["signups"]["first_touch"][0] if page else None,
                    }
                )
        content.sort(key=lambda r: -r["views"])

    audit = attach_audit(ctx, inputs, previous, objects, reasons, today)
    if not returned:
        raise RuntimeError("No provider read returned data; the previous snapshot stays in place.")

    pages = [objects[k] for k in detail]
    more_rows = [short(objects[k]) for k in more]
    dropped_rows = [short(objects[k]) for k in dropped]
    entry_rows = []
    if posthog.get("P1") and not posthog["P1"][1]:
        entries = [k for k in landing if k not in objects and k not in current]
        entries.sort(key=lambda k: landing[k]["current"]["sessions"], reverse=True)
        for key in entries[:15]:
            page = blank(key, window["current"][0])
            page["visits"] = landing[key]
            entry_rows.append(short(page))
    rest = {
        "search": {"pages": None, "clicks": None, "impressions": None},
        "visits": {"paths": None, "sessions": None},
    }
    listed = [k for k in objects if k in current]
    if totals["current"] and complete_current:
        rest["search"] = {
            "pages": max(0, len(current) - len(listed)),
            "clicks": max(0, totals["current"][0] - sum(current[k][0] for k in listed)),
            "impressions": max(0, totals["current"][1] - sum(current[k][1] for k in listed)),
        }
    if posthog.get("P1") and not posthog["P1"][1]:
        seen = [k for k in objects if k in landing]
        rest["visits"] = {
            "paths": max(0, len(landing) - len(seen)),
            "sessions": max(
                0,
                visits["current"]["sessions"]
                - sum(landing[k]["current"]["sessions"] for k in seen),
            ),
        }
    queried = [objects[k] for k in detail if objects[k]["search"]["hidden_clicks"] is not None]
    if queried:
        totals["branded_clicks"] = (
            sum(p["search"]["branded_clicks"] or 0 for p in queried) if brands else None
        )
        totals["other_named_clicks"] = sum(p["search"]["other_named_clicks"] for p in queried)
        totals["hidden_clicks"] = sum(p["search"]["hidden_clicks"] for p in queried)
    clicks = totals["current"][0] if totals["current"] else None
    ratio = None
    if clicks and visits["current"]["by_channel"]:
        ratio = round(visits["current"]["by_channel"][KEYS.index("Search")] / clicks, 4)

    # The readout: the same reads, as the founder's weekly Markdown.
    falling = []
    for page in pages:
        now_, before_ = page["search"]["current"], page["search"]["prior"]
        if now_ and before_ and before_[0] >= 20 and now_[0] <= 0.8 * before_[0]:
            falling.append(("/" + page["page"].split("/", 1)[1], now_[0], before_[0]))
    falling.sort(key=lambda r: r[1] - r[2])
    landing_rows = sorted(
        ((path, *cell) for path, cell in landing_week.items() if cell[0] or cell[1]),
        key=lambda r: -r[1],
    )
    exclusion_words = [
        f"emails at {d}" for rule in rules if rule["op"] == "suffix" for d in rule["value"]
    ] + [f"{rule['property']} true" for rule in rules if rule["op"] == "truthy"]
    markdown, title, growth = build(
        {
            "today": today,
            "weeks": window["weeks"],
            "notes": reasons,
            "min_count": settings["min_count"],
            "optional_rules": settings["optional_rules"],
            "launch_dates": settings["launch_dates"],
            "content_paths": settings["content_paths"],
            "activation": activation,
            "activation_days": days,
            "signup": signup,
            "paid_event": paid,
            "previous": (previous or {}).get("growth"),
            "channels": channel_week,
            "referrers": referrers,
            "assistants": assistants,
            "first_touch": touch,
            "signups_week": signups_week,
            "answers": answers,
            "search": {
                "weeks": [w for w in weeks if w["clicks"] is not None][:6]
                if len(weeks) > 1
                and weeks[0]["clicks"] is not None
                and weeks[1]["clicks"] is not None
                else [],
                "months": months,
                "reliable_from": str(reliable_from) if reliable_from else None,
                "totals": totals,
                "new_pages": [p["page"] for p in pages if "new_in_search" in p["flags"]],
                "falling": falling,
            },
            "content": content,
            "landing": (landing_rows if posthog.get("P1") and not posthog["P1"][1] else None),
            "audit": audit,
            "calls": [c["step"] for c in calls if c["outcome"] in ("ok", "truncated")],
            "exclusions": "; ".join(exclusion_words) or "none; team visits are counted",
        }
    )

    snapshot = {
        "schema": SCHEMA,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "run_id": (ctx.get("run_id") if isinstance(ctx, dict) else getattr(ctx, "run_id", None)),
        "status": "partial" if reasons or any(c["outcome"] != "ok" for c in calls) else "complete",
        "status_reasons": reasons,
        "windows": window,
        "definitions": {
            "channel_map": "product.analytics_brief channels",
            "channels": KEYS,
            "sessions": "PostHog $session_id, counted at the window and page of its first pageview",
            "exclusions": rules,
            "website_hosts": hosts,
            "website_hosts_source": host_source,
            "brand_terms": brands,
            "brand_source": brand_source,
            "signup_event": signup,
            "activation_event": activation,
            "activation_window_days": days,
            "paid_event": paid,
            "engaged_read": ENGAGED,
            "impressions_reliable_from": str(reliable_from) if reliable_from else None,
            "search_columns": ["clicks", "impressions", "ctr", "position"],
            "query_columns": ["query", "clicks", "impressions", "position", "branded"],
            "short_columns": SHORT,
        },
        "calls": calls,
        "totals": {
            "search": totals,
            "visits": visits,
            "signups": signup_totals,
            "activated": active_totals,
            "reading": reading,
            "search_sessions_per_click": ratio,
        },
        "search_weeks": weeks,
        "search_months": months,
        "pages": pages,
        "more_pages": more_rows,
        "entry_only_pages": entry_rows,
        "dropped_pages": dropped_rows,
        "rest": rest,
        "audit": audit,
        "previous": {k: previous.get(k) for k in ("generated_at", "windows", "totals")}
        if isinstance(previous, dict)
        else None,
        "readout": {"schema": READOUT_SCHEMA, "title": title, "markdown": markdown},
        "growth": growth,
        "trimmed": {
            "queries_per_page": 5,
            "short_rows_dropped": 0,
            "pages_moved_to_short": 0,
            "readout": "complete",
        },
        "notes": ["Search Console credits a click to the canonical URL; PostHog records the URL."],
    }

    def encode():
        return json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)

    content_text = encode()
    if len(content_text.encode()) > 63000:
        for page in pages:
            page["search"]["queries"] = page["search"]["queries"][:3]
        snapshot["trimmed"]["queries_per_page"] = 3
        content_text = encode()
    while len(content_text.encode()) > 63000 and (more_rows or entry_rows or dropped_rows):
        [rows for rows in (more_rows, entry_rows, dropped_rows) if rows][-1].pop()
        snapshot["trimmed"]["short_rows_dropped"] += 1
        content_text = encode()
    if len(content_text.encode()) > 63000:
        # The data is the contract other workflows read; the readout keeps its decisions.
        cut = markdown.split("\n## Where people came from", 1)[0]
        snapshot["readout"]["markdown"] = cut + (
            "\n\nThe rest of this week's readout was left out to keep the data file under 64 KB.\n"
        )
        snapshot["trimmed"]["readout"] = "decisions only"
        content_text = encode()
    while len(content_text.encode()) > 63000 and pages:
        more_rows.append(short(pages.pop()))
        snapshot["trimmed"]["pages_moved_to_short"] += 1
        content_text = encode()
    if len(content_text.encode()) > 64000:
        raise ValueError("The snapshot exceeds 64000 bytes.")
    return {"path": OUT, "content": content_text}
