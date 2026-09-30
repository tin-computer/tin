"""Weekly traffic snapshot: Search Console and PostHog per landing page, in one data file.

No judgments and no model. Other workflows read the file (organic.content_efficacy,
growth.acquisition_analytics) instead of querying the same providers again. Every number
is a bounded aggregate read; a failed or partial read leaves its fields null and names the
step in `status_reasons`, never a zero.

Dates follow Search Console, which reports days in Pacific time: the 28-day windows end on
Search Console's last final day, and the PostHog reads use the same Pacific dates.
"""

import datetime as dt
import json
import re
import time
from urllib.parse import urlsplit

from channels import CHANNELS, EXTRA, hogql, quote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

OUT = "analytics/traffic-snapshot.json"
SCHEMA = "tin.traffic_snapshot/1"
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
    return {
        "last_final_date": str(last),
        "current": [str(last - dt.timedelta(days=27)), str(last)],
        "prior": [str(last - dt.timedelta(days=55)), str(last - dt.timedelta(days=28))],
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
        email = "lowerUTF8(ifNull(toString(person.properties['email']), ''))"
        clauses.append(
            "NOT ("
            + " OR ".join(
                f"endsWith({email}, {quote('@' + d)}) OR endsWith({email}, {quote('.' + d)})"
                for d in clean
            )
            + ")"
        )
    if flag:
        rules.append({"property": flag, "op": "truthy"})
        column = f"lowerUTF8(ifNull(toString({reference(flag)}), ''))"
        clauses.append(f"NOT ({column} IN ('true', '1'))")
    if not rules:
        reasons.append(
            "step 2: no team email domains or internal flag were given, so team visits are counted."
        )
    return rules, " AND ".join(clauses) or "1 = 1"


def queries(windows_, hosts, excluded, signup, activation, days, engaged):
    """The three PostHog reads: landing sessions, signups by first touch, reading depth."""
    current, prior = windows_["current"], windows_["prior"]
    day = f"toDate(toTimeZone(timestamp, '{TIMEZONE}'))"
    scope = "(" + " OR ".join(f"lowerUTF8(toString(properties.$host)) = {quote(h)}" for h in hosts)
    scope += ")"
    period = (
        f"multiIf({day} BETWEEN toDate('{current[0]}') AND toDate('{current[1]}'), 'current', "
        f"{day} BETWEEN toDate('{prior[0]}') AND toDate('{prior[1]}'), 'prior', '')"
    )
    span = f"{day} BETWEEN toDate('{prior[0]}') AND toDate('{current[1]}')"
    entry = hogql(
        "toString(properties.utm_medium)",
        "toString(properties.$referring_domain)",
        "toString(properties.utm_source)",
        hosts,
    )
    # One row per PostHog session ($session_id), as PostHog counts sessions. Grouping by
    # (distinct_id, session) counted a session twice when a visitor signed in mid-session and
    # their distinct ID changed. A session belongs to the window and landing page of its first
    # pageview. Pageviews without a session ID count as pageviews, never as sessions.
    landing = (
        "WITH v AS (SELECT if(ifNull(toString(properties.$session_id), '') = '', "
        "concat('~', toString(distinct_id)), toString(properties.$session_id)) AS session, "
        f"timestamp, {period} AS w, toString(properties.$pathname) AS path, {entry} AS channel "
        f"FROM events WHERE event = '$pageview' AND {scope} AND {excluded} AND {span}), "
        "e AS (SELECT session, argMin(w, timestamp) AS w, argMin(path, timestamp) AS path, "
        "argMin(channel, timestamp) AS channel, count() AS pageviews, "
        "startsWith(session, '~') AS sessionless FROM v GROUP BY session) "
        "SELECT path, w, channel, countIf(NOT sessionless) AS sessions, "
        "sum(pageviews) AS pageviews FROM e GROUP BY path, w, channel "
        "ORDER BY sessions DESC LIMIT 400"
    )
    first_touch = hogql(
        "toString(person.properties.$initial_utm_medium)",
        "toString(person.properties.$initial_referring_domain)",
        "toString(person.properties.$initial_utm_source)",
        hosts,
    )
    active = quote(activation or "__none__")
    signups = (
        "WITH x AS (SELECT person_id, timestamp, event, "
        f"person.properties.$initial_pathname AS path, {first_touch} AS channel FROM events "
        f"WHERE event IN ({quote(signup)}, {active}) AND {excluded} "
        "AND timestamp >= now() - INTERVAL 180 DAY), "
        f"s AS (SELECT person_id, minIf(timestamp, event = {quote(signup)}) AS signup_at, "
        f"minIf(timestamp, event = {active}) AS active_at, "
        f"argMinIf(path, timestamp, event = {quote(signup)}) AS path, "
        f"argMinIf(channel, timestamp, event = {quote(signup)}) AS channel FROM x "
        "GROUP BY person_id) "
        f"SELECT path, channel, multiIf(toDate(toTimeZone(signup_at, '{TIMEZONE}')) BETWEEN "
        f"toDate('{current[0]}') AND toDate('{current[1]}'), 'current', 'prior') AS w, "
        "count() AS signups, "
        f"countIf(active_at >= signup_at AND active_at <= signup_at + INTERVAL {days} DAY) "
        "AS activated, "
        f"countIf(active_at < signup_at AND now() < signup_at + INTERVAL {days} DAY) AS open "
        "FROM s WHERE signup_at > toDateTime('1970-01-01') AND "
        f"toDate(toTimeZone(signup_at, '{TIMEZONE}')) BETWEEN toDate('{prior[0]}') AND "
        f"toDate('{current[1]}') GROUP BY path, channel, w ORDER BY signups DESC LIMIT 400"
    )
    reading = (
        "SELECT toString(properties.$prev_pageview_pathname) AS path, count() AS reads, "
        "median(toFloat64OrNull(toString(properties.$prev_pageview_duration))) AS median_seconds, "
        "countIf(toFloat64OrNull(toString(properties.$prev_pageview_max_content_percentage)) >= "
        f"{engaged}) AS engaged, "
        "countIf(toFloat64OrNull(toString(properties.$prev_pageview_max_content_percentage)) >= "
        "0.9) AS finished "
        f"FROM events WHERE event = '$pageleave' AND {scope} AND {excluded} AND "
        f"{day} BETWEEN toDate('{current[0]}') AND toDate('{current[1]}') "
        "GROUP BY path ORDER BY reads DESC LIMIT 400"
    )
    return {"P1": landing, "P2": signups, "P3": reading}


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


def attach_audit(ctx, inputs, previous, objects, reasons):
    """Pin the organic audit's findings to the pages they name."""
    known = ((previous or {}).get("audit") or {}).get("known_run_ids") or []
    audit = {
        "status": "none",
        "run_id": None,
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

    signup = str(inputs.get("signup_event") or "").strip() or None
    activation = str(inputs.get("activation_event") or "").strip() or None
    for name, value in (("signup_event", signup), ("activation_event", activation)):
        if value and not EVENT.fullmatch(value):
            raise ValueError(f"{name} is not an event name.")
    days = int(inputs.get("activation_window_days") or 7)
    engaged = 0.5
    if not signup:
        reasons.append("step 1: no signup_event was given; signups are not measured.")
    if not activation:
        reasons.append("step 1: no activation_event was given; activation is not measured.")
    rules, excluded = exclusions(inputs, reasons)
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
        cap = arguments.get("row_limit") if service == "gsc" else 400
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

    # G4/G5: queries for listed pages with clicks, then pages with impressions and no clicks.
    clicked = [k for k in detail if current.get(k, [0])[0] > 0]
    unclicked = [k for k in detail if current.get(k, [0, 0])[1] > 0 and current.get(k)[0] == 0]
    for step, keys in (("G4", clicked), ("G5", unclicked)):
        expression = "|".join(re.escape("https://" + k) for k in keys)
        if not keys or len(expression) > 4096:
            calls.append(
                {
                    "step": step,
                    "operation": "search_analytics.read",
                    "outcome": "skipped",
                    "rows": 0,
                }
            )
            continue
        filters = [{"dimension": "page", "operator": "includingRegex", "expression": expression}]
        read = await call(
            step,
            "gsc",
            "search_analytics.read",
            search(["page", "query"], window["current"], filters),
        )
        if not read:
            continue
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
        for key in keys:
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

    # P1-P3: PostHog, scoped to the site's hosts and the team exclusions.
    posthog = {}
    sql = queries(window, hosts, excluded, signup, activation, days, engaged) if hosts else {}
    for step in ("P1", "P2", "P3"):
        if not hosts or (step == "P2" and not signup):
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
    landing = {}
    home = hosts[0] if hosts else ""
    if posthog.get("P1"):
        totals_by_window = {name: zero_visits() for name in ("current", "prior")}
        for row in posthog["P1"][0]:
            name, channel = row.get("w"), row.get("channel")
            key = page_key(home + str(row.get("path") or "/"))
            if name not in totals_by_window or channel not in KEYS or not key:
                continue
            sessions, views = int(row.get("sessions") or 0), int(row.get("pageviews") or 0)
            index = KEYS.index(channel)
            for bucket in (
                totals_by_window[name],
                landing.setdefault(key, {z: zero_visits() for z in ("current", "prior")})[name],
            ):
                bucket["sessions"] += sessions
                bucket["pageviews"] += views
                bucket["by_channel"][index] += sessions
        if not posthog["P1"][1]:
            visits = totals_by_window
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
    if posthog.get("P2"):
        rows, partial = posthog["P2"]
        if not any((number(r.get("signups")) or 0) > 0 for r in rows):
            reasons.append("step P2: the signup event was not seen in 180 days; check the name.")
        elif partial:
            reasons.append("step P2: the signup read was truncated; totals are not measured.")
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
            for row in rows:
                name, channel = row.get("w"), row.get("channel")
                if name not in ("current", "prior"):
                    continue
                slot = 0 if name == "current" else 1
                count = int(row.get("signups") or 0)
                signup_totals[name] += count
                if channel in KEYS:
                    signup_totals["by_channel"][name][KEYS.index(channel)] += count
                if activation:
                    active_totals[name] += int(row.get("activated") or 0)
                    active_totals["open"] += int(row.get("open") or 0)
                key = page_key(home + str(row.get("path") or "/"))
                if key in objects:
                    page = objects[key]
                    page["signups"]["first_touch"][slot] += count
                    if activation:
                        page["signups"]["activated"][slot] += int(row.get("activated") or 0)
                        page["signups"]["open"] += int(row.get("open") or 0)
    if posthog.get("P3"):
        rows, partial = posthog["P3"]
        if not rows:
            reasons.append("step P3: no pageleave events were seen; reading is not tracked.")
        elif not partial:
            reading = {"reads": 0, "engaged": 0, "finished": 0}
            for row in rows:
                reads, deep, done = (
                    int(row.get("reads") or 0),
                    int(row.get("engaged") or 0),
                    int(row.get("finished") or 0),
                )
                reading["reads"] += reads
                reading["engaged"] += deep
                reading["finished"] += done
                key = page_key(home + str(row.get("path") or "/"))
                if key in objects:
                    objects[key]["reading"] = {
                        "reads": reads,
                        "median_seconds": number(row.get("median_seconds")),
                        "engaged": deep,
                        "finished": done,
                    }

    audit = attach_audit(ctx, inputs, previous, objects, reasons)
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
        totals["branded_clicks"] = sum(p["search"]["branded_clicks"] or 0 for p in queried)
        totals["other_named_clicks"] = sum(p["search"]["other_named_clicks"] for p in queried)
        totals["hidden_clicks"] = sum(p["search"]["hidden_clicks"] for p in queried)
    clicks = totals["current"][0] if totals["current"] else None
    ratio = None
    if clicks and visits["current"]["by_channel"]:
        ratio = round(visits["current"]["by_channel"][KEYS.index("Search")] / clicks, 4)

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
            "engaged_read": engaged,
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
        "trimmed": {"queries_per_page": 5, "short_rows_dropped": 0, "pages_moved_to_short": 0},
        "notes": ["Search Console credits a click to the canonical URL; PostHog records the URL."],
    }

    def encode():
        return json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)

    content = encode()
    if len(content.encode()) > 63000:
        for page in pages:
            page["search"]["queries"] = page["search"]["queries"][:3]
        snapshot["trimmed"]["queries_per_page"] = 3
        content = encode()
    while len(content.encode()) > 63000 and (more_rows or entry_rows or dropped_rows):
        [rows for rows in (more_rows, entry_rows, dropped_rows) if rows][-1].pop()
        snapshot["trimmed"]["short_rows_dropped"] += 1
        content = encode()
    while len(content.encode()) > 63000 and pages:
        more_rows.append(short(pages.pop()))
        snapshot["trimmed"]["pages_moved_to_short"] += 1
        content = encode()
    if len(content.encode()) > 64000:
        raise ValueError("The snapshot exceeds 64000 bytes.")
    return {"path": OUT, "content": content}
