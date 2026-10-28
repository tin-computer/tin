# Journeys resource

The reviewed code for every query, count and status in the report. Extract the single Python
block below into `journeys.py` in a scratch folder, import it, and use it unchanged. It builds
each `call_service` request, reads each response, sorts people into tiers, finds stalls and
frictions, carries the ledger forward and checks the finished report. You do the judgment:
the map's meanings, the journeys in words, causes, leads, actions and product changes.

The order of a run:

```text
c = settings(inputs)                      # inputs as given; refuses invalid ones
prev = previous(text_of_newest_report)    # or previous(None) on the first run
m = prev["map"] or default_map()
project.read     -> project_info(result)  # replay link and capture settings
inventory        -> inventory(result)     # on the first run and when check_drift says so
hosts            -> hosts(result)
                    write the map (your judgment), then m = check_map(m)
census           -> census(result)
prior            -> prior(result)
p = people(census_rows, prior_rows, c, m)
frictions        -> friction_rows(result)
loops            -> loop_rows(result)
paths            -> site_paths(path_rows(result), has_more)  # how visitors move through the site
timelines (1-3)  -> timelines(result)     # for choose(p, c["max_people_detailed"])
recordings       -> rec = recordings(result)  # which of their sessions PostHog recorded
                    journey(rows, m, rec); replay(m, session_id, rec)
funnel           -> fn = funnel(result, m, inventory_rows, c["period"], c["end"])
f = frictions(friction_rows, loop_rows, stalls(p, m), p)
web search       -> w = profile(...)      # each new person's work, when look_up_new_people
                    named(person, ..., work=w)
ledger = carry(prev["ledger"], p, f, c)
b = brief(observations, new, stages=...) # the founder's brief, in your words: only what is new
evidence = assemble(..., brief=b, funnel=fn, previous=prev)
text = report (render_brief(evidence["brief"]) under Status) + render_evidence(evidence)
check(text)
```

```python
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

VERSION = "visitor-journal.v1"
EVIDENCE_MARKER = "<!-- tin-visitor-journal-v1 -->"
# A report says what it has; a missing read is a limitation, not a lesser status.
STATUSES = ("complete", "nothing to report", "invalid configuration")
# Every person lands in exactly one tier. Only the first three are named in the report and
# written to People: they told the product who they are.
TIERS = ("lead", "new_user", "user", "browser", "passer_by", "account", "team", "bot")
NAMED_TIERS = ("lead", "new_user", "user")
KEY_KINDS = ("intent", "booking", "signup", "activation", "payment", "failure", "feedback")
FRICTION_KINDS = ("rage_click", "dead_click", "error", "failure", "stall", "loop", "other")
FRICTION_STATUSES = ("new", "ongoing", "fixed")
REQUEST_OUTCOMES = ("ok", "refused", "invalid")
OPERATIONS = ("project.read", "query.hogql", "event_definitions.list", "property_definitions.list")
SERVICE = "analytics"
MAX_LOOKBACK_HOURS = 168
PRIOR_DAYS = 90
INVENTORY_DAYS = 30
FIXED_AFTER_DAYS = 5
LEDGER_DAYS = 90
MAX_NAMED = 60
MAX_FRICTIONS = 30
MAX_TIMELINE_PEOPLE = 15
ANONYMOUS_JOURNEYS = 3
ROUTE_PAGES = 4
# A timeline reads each person's first and last events; the middle of a long day is skipped,
# never the end. 15 people at 60 rows stay under the 1000-row query limit.
TIMELINE_FIRST_EVENTS = 30
TIMELINE_LAST_EVENTS = 30
MAX_RECORDING_SESSIONS = 120  # one query, well under Tin's 8000-byte bound
SUMMARY_LIMIT = 300
# The brief at the top of the report is read on its own, in the founder's daily report.
FUNNEL_STEPS = 3
# A day compares yesterday with the last 7 days; a week, read carefully once a week, reads 7
# days against the 4 weeks before and keeps longer lines.
PERIODS = {
    "day": {"bucket": 1, "buckets": 7, "new": 5, "observations": 5, "line": 240},
    "week": {"bucket": 7, "buckets": 5, "new": 6, "observations": 5, "line": 300},
}
# The brief groups people instead of a line each: new users by how far they got, where people
# got stuck, and what returning users did. `new` is only the few worth knowing on their own.
GROUPS = ("stages", "stuck", "returning")
MAX_GROUPS = 6
GROUP_LABEL = 80
GROUP_NOTE = 160
ROWS = {
    "day": ("Yesterday", "Last 7 days, daily average"),
    "week": ("This week", "The 4 weeks before, weekly average"),
}
ALSO = {"day": "Also yesterday", "week": "Also this week"}
# Words from inside this resource that mean nothing to a founder.
INTERNAL_WORDS = re.compile(
    r"\b(census|ledger|timelines?|tiers?|quer(?:y|ies)|server-only|hogql)\b", re.IGNORECASE
)
ACTION_LIMIT = 300
REASON_LIMIT = 180
# Events that say nothing about a journey: PostHog's own bookkeeping.
QUIET_EVENTS = (
    "$pageleave",
    "$web_vitals",
    "$feature_flag_called",
    "$set",
    "$groupidentify",
    "$identify",
    "$create_alias",
    "$opt_in",
    "survey shown",
)
HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
EMAIL = re.compile(r"^[^@\s]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,63}$")
EVENT_NAME = re.compile(r"^[^\x00-\x1f'\\]{1,200}$")
PROPERTY = re.compile(r"^\$?[A-Za-z_][A-Za-z0-9_$]{0,99}$")
SESSION = re.compile(r"^[0-9A-Za-z-]{8,80}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LINKEDIN = re.compile(r"^https://(?:www\.)?linkedin\.com/in/[^/?#\s()<>]{1,100}/?$")
SITE = re.compile(r"^https://[a-z0-9.-]{1,253}\.[a-z]{2,63}(?:/[^\s()<>]{0,200})?$")


class ContractError(ValueError):
    """The input, a response or the report breaks this resource's contract."""


# ---------------------------------------------------------------- settings


def _now(now=None):
    value = now or datetime.now(UTC)
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def _iso(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _strings(values, field, pattern=None, limit=50, lower=True):
    if values in (None, ""):
        return []
    if not isinstance(values, list) or len(values) > limit:
        raise ContractError(f"{field} must be a list of at most {limit} values")
    out = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"{field} holds an empty or non-text value")
        value = value.strip().lower() if lower else value.strip()
        if pattern is not None and not pattern.fullmatch(value):
            raise ContractError(f"{field} holds an invalid value: {value[:60]}")
        if value not in out:
            out.append(value)
    return out


def _host(value):
    value = value.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/", 1)[0].split(":", 1)[0]


def settings(inputs, now=None):
    """The run's frozen window and the founder's settings, validated before any request."""
    inputs = dict(inputs or {})
    hours = inputs.get("lookback_hours", 24)
    if type(hours) is not int or not 1 <= hours <= MAX_LOOKBACK_HOURS:
        raise ContractError(f"lookback_hours must be 1-{MAX_LOOKBACK_HOURS}")
    period = inputs.get("period", "day")
    if period not in PERIODS:
        raise ContractError("period must be day or week")
    if period == "week":
        hours = 168
    detailed = inputs.get("max_people_detailed", 12)
    if type(detailed) is not int or not 1 <= detailed <= 30:
        raise ContractError("max_people_detailed must be 1-30")
    end = _now(now)
    hosts = {}
    for field in ("website_hosts", "product_hosts"):
        hosts[field] = _strings([_host(h) for h in inputs.get(field) or []], field, HOST, 5)
    internal = _strings(inputs.get("internal_people"), "internal_people", None, 50)
    notes = inputs.get("notes") or ""
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ContractError("notes must be text of at most 2000 characters")
    return {
        "end": _iso(end),
        "start": _iso(end - timedelta(hours=hours)),
        "prior_start": _iso(end - timedelta(hours=hours) - timedelta(days=PRIOR_DAYS)),
        "inventory_start": _iso(end - timedelta(days=INVENTORY_DAYS)),
        "lookback_hours": hours,
        "period": period,
        "max_people_detailed": detailed,
        "website_hosts": hosts["website_hosts"],
        "product_hosts": hosts["product_hosts"],
        "exclude_email_domains": _strings(
            inputs.get("exclude_email_domains"), "exclude_email_domains", HOST, 10
        ),
        "internal_people": internal,
        "notes": notes,
        "look_up_new_people": inputs.get("look_up_new_people") is not False,
        "today": end.strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------- the map


def default_map():
    """An empty map. The first run fills it from the inventory, hosts and project docs."""
    return {
        "version": 1,
        "hosts": {"website": [], "product": [], "ignore": []},
        "identity": {"email": "email", "name": "name"},
        "key_events": {kind: [] for kind in KEY_KINDS},
        "steps": [],
        "funnel": [],
        "internal": {"email_domains": [], "people": []},
        "bots": {"countries": [], "browsers": []},
        "replay_url": None,
        "capture": {},
        "changes": [],
    }


def check_map(m):
    """Validate the map you wrote; returns a normalized copy. Event names and properties go
    into HogQL as literals, so anything unsafe is refused."""
    if not isinstance(m, dict):
        raise ContractError("the map must be an object")
    base = default_map()
    out = {"version": 1}
    hosts = m.get("hosts") or {}
    out["hosts"] = {
        key: _strings([_host(h) for h in hosts.get(key) or []], f"hosts.{key}", HOST, 20)
        for key in ("website", "product")
    }
    # Ignored hosts may be whole families: "*.vercel.app" covers every preview deployment.
    ignore = [h.strip().lower() for h in hosts.get("ignore") or [] if isinstance(h, str)]
    out["hosts"]["ignore"] = _strings(
        [("*." + _host(h[2:])) if h.startswith("*.") else _host(h) for h in ignore],
        "hosts.ignore",
        re.compile(r"^(\*\.)?" + HOST.pattern.lstrip("^")),
        20,
    )
    identity = m.get("identity") or base["identity"]
    out["identity"] = {}
    for key in ("email", "name"):
        value = identity.get(key) or base["identity"][key]
        if not isinstance(value, str) or not PROPERTY.fullmatch(value):
            raise ContractError(f"identity.{key} must be a person property name")
        out["identity"][key] = value
    events = m.get("key_events") or {}
    out["key_events"] = {}
    for kind in KEY_KINDS:
        out["key_events"][kind] = _strings(
            events.get(kind) or [], f"key_events.{kind}", EVENT_NAME, 12, lower=False
        )
    unknown = set(events) - set(KEY_KINDS)
    if unknown:
        raise ContractError(f"key_events has unknown kinds: {sorted(unknown)}")
    every = {e for names in out["key_events"].values() for e in names}
    out["steps"] = _strings(m.get("steps") or [], "steps", EVENT_NAME, 8, lower=False)
    if any(step not in every and step != "$pageview" for step in out["steps"]):
        raise ContractError("every step must be a key event")
    out["funnel"] = _funnel_steps(m.get("funnel") or [], every)
    internal = m.get("internal") or {}
    out["internal"] = {
        "email_domains": _strings(
            internal.get("email_domains"), "internal.email_domains", HOST, 20
        ),
        "people": _strings(internal.get("people"), "internal.people", None, 50),
    }
    bots = m.get("bots") or {}
    out["bots"] = {
        "countries": _strings(
            bots.get("countries"), "bots.countries", re.compile(r"^[a-z]{2}$"), 30
        ),
        "browsers": _strings(bots.get("browsers"), "bots.browsers", None, 10),
    }
    replay = m.get("replay_url")
    if replay is not None and not (
        isinstance(replay, str)
        and re.fullmatch(r"https://(us|eu)\.posthog\.com/project/\d{1,12}/replay/", replay)
    ):
        raise ContractError("replay_url must come from project_info()")
    out["replay_url"] = replay
    capture = m.get("capture") or {}
    out["capture"] = {
        k: v for k, v in capture.items() if isinstance(k, str) and isinstance(v, bool)
    }
    changes = m.get("changes") or []
    if not isinstance(changes, list):
        raise ContractError("changes must be a list")
    out["changes"] = [
        {"date": str(c.get("date", ""))[:10], "change": str(c.get("change", ""))[:300]}
        for c in changes[-40:]
        if isinstance(c, dict) and c.get("change")
    ]
    if len(json.dumps(out)) > 24000:
        raise ContractError("the map is too large; keep only what the queries use")
    return out


def _funnel_steps(steps, every):
    """The brief's table: visitors, then an optional middle step, then the main conversion.
    A site that records nothing past a visit has visitors alone. Chosen once and kept, so
    each day reads against the same steps."""
    if not isinstance(steps, list):
        raise ContractError("funnel must be a list")
    if not steps:
        return []
    out = []
    for step in steps:
        label = step.get("label") if isinstance(step, dict) else None
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or "\n" in label:
            raise ContractError("each funnel step needs a label of at most 40 characters")
        events = _strings(step.get("events") or [], "funnel.events", EVENT_NAME, 6, lower=False)
        if not events or any(e not in every and e != "$pageview" for e in events):
            raise ContractError("each funnel step needs events from key_events")
        out.append({"label": label.strip(), "events": events})
    if len(out) > FUNNEL_STEPS or out[0]["events"] != ["$pageview"]:
        raise ContractError(
            "the funnel starts with visitors ($pageview) and has at most three steps"
        )
    return out


def key_event_names(m):
    names = []
    for kind in KEY_KINDS:
        for name in m["key_events"][kind]:
            if name not in names:
                names.append(name)
    return names


def kind_of(m, event):
    return [kind for kind in KEY_KINDS if event in m["key_events"][kind]]


# ---------------------------------------------------------------- requests


def _literal(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _list(values):
    return "(" + ", ".join(_literal(v) for v in values) + ")"


def _window(start, end):
    return f"timestamp >= toDateTime({_literal(start)}) AND timestamp < toDateTime({_literal(end)})"


def _scope(c, m):
    """Pageviews on the chosen hosts; server events (no host) always count."""
    hosts = list(
        dict.fromkeys(
            c["website_hosts"] + c["product_hosts"] + m["hosts"]["website"] + m["hosts"]["product"]
        )
    )
    exact = [h for h in m["hosts"]["ignore"] if not h.startswith("*.") and h not in hosts]
    families = [h[1:] for h in m["hosts"]["ignore"] if h.startswith("*.")]
    if hosts:
        return f"(properties.$host IN {_list(hosts)} OR properties.$host IS NULL)"
    kept = [f"properties.$host NOT IN {_list(exact)}"] if exact else []
    kept += [f"NOT endsWith(properties.$host, {_literal(f)})" for f in families]
    if not kept:
        return "1 = 1"
    return f"(({' AND '.join(kept)}) OR properties.$host IS NULL)"


def _person_prop(m, key):
    return f"person.properties.{m['identity'][key]}"


def _exclusions(c, m):
    """Team email domains drop at the query; named team people drop in people()."""
    domains = list(dict.fromkeys(c["exclude_email_domains"] + m["internal"]["email_domains"]))
    if not domains:
        return "1 = 1"
    email = f"lower(coalesce(toString({_person_prop(m, 'email')}), ''))"
    return (
        "NOT ("
        + " OR ".join(
            f"endsWith({email}, {_literal('@' + d)}) OR endsWith({email}, {_literal('.' + d)})"
            for d in domains
        )
        + ")"
    )


def _message(width):
    return (
        "left(coalesce(toString(properties.$exception_values), properties.$exception_message,"
        " properties.error, properties.reason, properties.message, if(isNull(properties.status),"
        f" NULL, concat('status ', toString(properties.status))), ''), {width}) AS message"
    )


def _reached(names):
    return f"countIf(event IN {_list(names)}) > 0" if names else "0"


def _payload(step, operation, arguments):
    return {"service": SERVICE, "step": step, "operation": operation, "arguments": arguments}


def _query(step, sql):
    sql = " ".join(sql.split())
    if len(sql.encode()) > 8000:
        raise ContractError(f"the {step} query is over Tin's 8000-byte bound; narrow the map")
    return _payload(step, "query.hogql", {"query": sql, "name": f"visitors {step}"})


def request(c, m, step, person_ids=None, session_ids=None):
    """The call_service payload for one step. Pass it to call_service unchanged."""
    w = _window(c["start"], c["end"])
    scope, keep = _scope(c, m), _exclusions(c, m)
    keys = key_event_names(m)
    if step == "project":
        return _payload("project", "project.read", {})
    if step == "inventory":
        return _query(
            "inventory",
            f"""SELECT event, count() AS n, uniq(person_id) AS people,
            min(timestamp) AS first_at, max(timestamp) AS last_at FROM events
            WHERE {_window(c["inventory_start"], c["end"])}
            GROUP BY event ORDER BY n DESC LIMIT 300""",
        )
    if step == "hosts":
        return _query(
            "hosts",
            f"""SELECT properties.$host AS host, count() AS pageviews,
            uniq(person_id) AS people FROM events
            WHERE {_window(c["inventory_start"], c["end"])} AND event = '$pageview'
            GROUP BY host ORDER BY pageviews DESC LIMIT 60""",
        )
    if step == "census":
        tracked = keys + ["$rageclick", "$dead_click", "$exception"]
        return _query(
            "census",
            f"""SELECT person_id,
            any({_person_prop(m, "email")}) AS email, any({_person_prop(m, "name")}) AS name,
            min(timestamp) AS first_seen, max(timestamp) AS last_seen,
            uniq(properties.$session_id) AS sessions,
            countIf(event = '$pageview') AS pageviews,
            uniqIf(properties.$pathname, event = '$pageview') AS paths,
            groupUniqArrayIf(12)(properties.$pathname, event = '$pageview') AS path_sample,
            argMinIf(properties.$pathname, timestamp, event = '$pageview') AS entry_path,
            argMaxIf(properties.$pathname, timestamp, event = '$pageview') AS last_path,
            argMin(properties.$referring_domain, timestamp) AS referrer,
            argMin(properties.utm_source, timestamp) AS utm_source,
            argMin(properties.utm_campaign, timestamp) AS utm_campaign,
            any(person.properties.$initial_referring_domain) AS first_touch,
            any(properties.$geoip_country_code) AS country,
            any(properties.$geoip_city_name) AS city,
            any(properties.$device_type) AS device, any(properties.$browser) AS browser,
            groupUniqArrayIf(30)(event, event IN {_list(tracked)}) AS key_events,
            countIf(event = '$rageclick') AS rage_clicks,
            countIf(event = '$dead_click') AS dead_clicks,
            countIf(event = '$exception') AS errors,
            groupUniqArray(5)(properties.$host) AS hosts,
            groupUniqArray(5)(properties.$session_id) AS session_ids
            FROM events WHERE {w} AND {scope} AND {keep}
            AND event NOT IN {_list(QUIET_EVENTS)}
            GROUP BY person_id HAVING pageviews > 0 OR length(key_events) > 0
            ORDER BY length(key_events) * 5 + pageviews DESC, last_seen DESC LIMIT 1000""",
        )
    if step == "prior":
        return _query(
            "prior",
            f"""SELECT person_id,
            uniq(properties.$session_id) AS prior_sessions,
            uniq(toDate(timestamp)) AS prior_days, min(timestamp) AS first_ever,
            max(timestamp) AS last_before
            FROM events WHERE {_window(c["prior_start"], c["start"])}
            AND person_id IN (SELECT person_id FROM events WHERE {w} AND {scope})
            GROUP BY person_id LIMIT 1000""",
        )
    if step == "frictions":
        failures = m["key_events"]["failure"]
        failing = f" OR event IN {_list(failures)}" if failures else ""
        return _query(
            "frictions",
            f"""SELECT event, properties.$pathname AS path,
            left(coalesce(properties.$el_text, elements_chain_href, ''), 80) AS element,
            {_message(160)},
            count() AS n, uniq(person_id) AS people, uniq(properties.$session_id) AS sessions,
            min(timestamp) AS first_at, max(timestamp) AS last_at,
            groupUniqArray(10)(toString(person_id)) AS person_ids,
            groupUniqArray(3)(properties.$session_id) AS session_ids
            FROM events WHERE {w} AND {scope} AND {keep}
            AND (event IN ('$rageclick', '$dead_click', '$exception'){failing})
            GROUP BY event, path, element, message
            ORDER BY people DESC, n DESC LIMIT 200""",
        )
    if step == "loops":
        return _query(
            "loops",
            f"""SELECT path, uniq(pid) AS people, count() AS sessions,
            max(views) AS most, groupUniqArray(10)(toString(pid)) AS person_ids FROM (
            SELECT person_id AS pid, properties.$session_id AS sid, properties.$pathname AS path,
            count() AS views FROM events WHERE {w} AND {scope} AND {keep}
            AND event = '$pageview' GROUP BY pid, sid, path HAVING views >= 3)
            GROUP BY path ORDER BY people DESC LIMIT 50""",
        )
    if step == "paths":
        # One row per route visitors took on the website, a session at a time: where they came
        # in, the first pages in order, where they left, and how far they got. Product pages
        # stay out when the map names the website's hosts.
        site = list(dict.fromkeys(c["website_hosts"] + m["hosts"]["website"]))
        pages = f"properties.$host IN {_list(site)}" if site else scope
        kept = keep
        ids = [x for x in c["internal_people"] + m["internal"]["people"] if UUID.fullmatch(x)]
        if ids:
            kept += f" AND toString(person_id) NOT IN {_list(ids)}"
        if m["bots"]["countries"]:
            kept += (
                " AND lower(coalesce(properties.$geoip_country_code, ''))"
                f" NOT IN {_list(m['bots']['countries'])}"
            )
        intent = m["key_events"]["intent"] + m["key_events"]["booking"] + m["key_events"]["payment"]
        signup = m["key_events"]["signup"]
        steps = f" OR event IN {_list(intent + signup)}" if intent + signup else ""
        return _query(
            "paths",
            f"""SELECT pages[1] AS entry,
            arrayStringConcat(arraySlice(pages, 1, {ROUTE_PAGES}), ' > ') AS route,
            pages[length(pages)] AS exit, count() AS sessions, uniq(pid) AS people,
            countIf(known) AS known, round(avg(views), 1) AS pageviews,
            countIf(views = 1) AS bounced, countIf(intent) AS intent, countIf(signup) AS signup,
            countIf(rage) AS rage FROM (
            SELECT person_id AS pid, properties.$session_id AS sid,
            arrayCompact(arrayMap(x -> x.2, arraySort(x -> x.1,
              groupArrayIf((timestamp, properties.$pathname), event = '$pageview')))) AS pages,
            countIf(event = '$pageview') AS views,
            max(coalesce(toString({_person_prop(m, "email")}), '') != '') AS known,
            {_reached(intent)} AS intent, {_reached(signup)} AS signup,
            countIf(event = '$rageclick') > 0 AS rage
            FROM events WHERE {w} AND {kept}
            AND ((event IN ('$pageview', '$rageclick') AND {pages}){steps})
            GROUP BY pid, sid HAVING views > 0)
            GROUP BY entry, route, exit ORDER BY sessions DESC, route LIMIT 1000""",
        )
    if step == "funnel":
        if not m["funnel"]:
            raise ContractError("choose the map's funnel before asking for it")
        end = datetime.strptime(c["end"], "%Y-%m-%d %H:%M:%S")
        span = PERIODS[c["period"]]
        start = end - timedelta(days=span["bucket"] * span["buckets"])
        start = start.strftime("%Y-%m-%d %H:%M:%S")
        team = m["internal"]["people"]
        others = f" AND toString(person_id) NOT IN {_list(team)}" if team else ""
        counted = [
            "uniqIf(person_id, event = '$pageview')"
            if f["events"] == ["$pageview"]
            else f"uniqIf(person_id, event IN {_list(f['events'])})"
            for f in m["funnel"]
        ]
        counted += ["0"] * (FUNNEL_STEPS - len(counted))
        return _query(
            "funnel",
            f"""SELECT intDiv(dateDiff('second', timestamp, toDateTime({_literal(c["end"])})),
            {86400 * span["bucket"]}) AS ago, {counted[0]} AS step1, {counted[1]} AS step2, {counted[2]} AS step3
            FROM events WHERE {_window(start, c["end"])} AND {scope} AND {keep}{others}
            GROUP BY ago ORDER BY ago LIMIT {span["buckets"]}""",
        )
    if step.startswith("timelines"):
        ids = [str(p) for p in (person_ids or [])]
        if not 1 <= len(ids) <= MAX_TIMELINE_PEOPLE or any(not UUID.fullmatch(p) for p in ids):
            raise ContractError(
                f"timelines take 1-{MAX_TIMELINE_PEOPLE} person IDs from the census"
            )
        return _query(
            step,
            f"""SELECT person, timestamp, event, session, host, path, element, message, scroll,
            n, total FROM (
            SELECT toString(person_id) AS person, timestamp, event,
            properties.$session_id AS session, properties.$host AS host,
            properties.$pathname AS path,
            left(coalesce(properties.$el_text, elements_chain_href, ''), 60) AS element,
            {_message(120)},
            properties.$prev_pageview_max_content_percentage AS scroll,
            row_number() OVER (PARTITION BY person_id ORDER BY timestamp) AS n,
            count() OVER (PARTITION BY person_id) AS total
            FROM events WHERE {w} AND toString(person_id) IN {_list(ids)}
            AND event NOT IN {_list(QUIET_EVENTS)}
            AND NOT (event = '$autocapture' AND coalesce(properties.$event_type, '') NOT IN ('click', 'submit')))
            WHERE n <= {TIMELINE_FIRST_EVENTS} OR n > total - {TIMELINE_LAST_EVENTS}
            ORDER BY person, n LIMIT 1000""",
        )
    if step == "recordings":
        # Only a session in the replay table has a replay; a site's SDK may not record.
        ids = list(dict.fromkeys(str(s) for s in (session_ids or [])))
        if not 1 <= len(ids) <= MAX_RECORDING_SESSIONS or not all(map(SESSION.fullmatch, ids)):
            raise ContractError(f"recordings take 1-{MAX_RECORDING_SESSIONS} session IDs")
        return _query(
            "recordings",
            f"""SELECT session_id FROM raw_session_replay_events
            WHERE min_first_timestamp >= toDateTime({_literal(c["start"])}) - INTERVAL 1 DAY
            AND session_id IN {_list(ids)} GROUP BY session_id LIMIT 1000""",
        )
    raise ContractError(f"unknown step {step}")


# ---------------------------------------------------------------- responses


def table(result, columns):
    """Rows of one query.hogql result as dicts, after checking its columns."""
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        raise ContractError("not a query.hogql result")
    if result.get("columns") != list(columns):
        raise ContractError(f"unexpected columns {result.get('columns')}")
    return [dict(zip(columns, row)) for row in result["rows"]]


def outcome(step, operation, result=None, error=None):
    """One request's line in the evidence. A refused or failed call is never zero."""
    row = {"step": step, "operation": operation, "outcome": "ok", "rows": 0, "truncated": False}
    if error is not None:
        return {**row, "outcome": "refused", "note": str(error)[:200]}
    rows = result.get("rows") if isinstance(result, dict) else None
    row["rows"] = len(rows) if isinstance(rows, list) else 1
    row["truncated"] = isinstance(result, dict) and bool(
        result.get("truncated") or result.get("has_more")
    )
    return row


INVENTORY = "event n people first_at last_at".split()
HOSTS = "host pageviews people".split()
CENSUS = "person_id email name first_seen last_seen sessions pageviews paths path_sample entry_path last_path referrer utm_source utm_campaign first_touch country city device browser key_events rage_clicks dead_clicks errors hosts session_ids".split()
PRIOR = "person_id prior_sessions prior_days first_ever last_before".split()
FRICTIONS = (
    "event path element message n people sessions first_at last_at person_ids session_ids".split()
)
LOOPS = "path people sessions most person_ids".split()
PATHS = "entry route exit sessions people known pageviews bounced intent signup rage".split()
RECORDINGS = ["session_id"]
TIMELINE = "person timestamp event session host path element message scroll n total".split()

FUNNEL = "ago step1 step2 step3".split()


def funnel(result, m, inventory_rows=None, period="day", end=None):
    """The brief's table: people per step yesterday against the 7-day daily average, or this
    week (`yesterday`) against the weekly average of the 4 weeks before. A step whose events
    never fired in the 30-day inventory is not tracked, not zero. When the inventory's first
    visit comes after its first day, tracking began then: the average leaves out the days or
    weeks before it, and with none left there is no earlier data (`average` None)."""
    span = PERIODS[period]
    n = span["buckets"]
    rows = {int(row["ago"]): row for row in table(result, FUNNEL) if 0 <= int(row["ago"]) < n}
    seen = {row["event"] for row in inventory_rows} if inventory_rows is not None else None
    began, stop = _tracking_began(inventory_rows, end), _moment(end)
    # A day or week counts only when tracking was on for all of it.
    before = [
        ago
        for ago in range(0 if period == "day" else 1, n)
        if began is None or stop - timedelta(days=span["bucket"] * (ago + 1)) >= began
    ]
    steps = []
    for i, step in enumerate(m["funnel"]):
        key = f"step{i + 1}"
        tracked = seen is None or any(e in seen for e in step["events"])
        each = [int((rows.get(ago) or {}).get(key) or 0) for ago in range(n)]
        average = round(sum(each[a] for a in before) / len(before), 1) if before else None
        steps.append(
            {
                "label": step["label"],
                "yesterday": each[0] if tracked else None,
                "average": average if tracked else None,
            }
        )
    return {"period": period, "days": len(before) * span["bucket"], "steps": steps}


def _tracking_began(inventory_rows, end):
    """When the first visit in the inventory came, if tracking began within it; None when it
    began before the inventory's first day or cannot be read."""
    stop = _moment(end)
    if not inventory_rows or stop is None:
        return None
    firsts = [
        _moment(row.get("first_at")) for row in inventory_rows if row.get("event") == "$pageview"
    ]
    first = min((x for x in firsts if x is not None), default=None)
    if first is None or first < stop - timedelta(days=INVENTORY_DAYS - 1):
        return None
    return first


def project_info(result):
    """What project.read says about capture: the replay link and what is switched on."""
    if not isinstance(result, dict) or "replay_url" not in result:
        raise ContractError("not a project.read result")
    capture = {
        k: v
        for k, v in result.items()
        if isinstance(v, bool) and k not in ("has_more", "truncated")
    }
    return {
        "replay_url": result.get("replay_url"),
        "capture": capture,
        "app_urls": [u for u in result.get("app_urls") or [] if isinstance(u, str)],
    }


def inventory(result):
    return table(result, INVENTORY)


def hosts(result):
    return table(result, HOSTS)


def census(result):
    return table(result, CENSUS)


def prior(result):
    return table(result, PRIOR)


def friction_rows(result):
    return table(result, FRICTIONS)


def loop_rows(result):
    return table(result, LOOPS)


def path_rows(result):
    return table(result, PATHS)


def recordings(result):
    """The sessions PostHog kept a recording of. Pass it to journey() and replay()."""
    return frozenset(
        str(row["session_id"]) for row in table(result, RECORDINGS) if row.get("session_id")
    )


def timelines(result):
    by = {}
    for row in table(result, TIMELINE):
        by.setdefault(row["person"], []).append(row)
    return by


def check_drift(m, inventory_rows):
    """{"silent": key events that stopped firing, "new": events that look like renames}.
    Either means re-map before counting, and record the change in the map."""
    seen = {row["event"]: row for row in inventory_rows}
    silent = [name for name in key_event_names(m) if name not in seen]
    words = {w for name in silent for w in re.split(r"[_\s.:-]+", name.lower()) if len(w) > 3}
    new = [
        row["event"]
        for row in inventory_rows
        if not row["event"].startswith("$")
        and row["event"] not in key_event_names(m)
        and words & set(re.split(r"[_\s.:-]+", row["event"].lower()))
    ]
    return {"silent": silent, "new": new[:20]}


# ---------------------------------------------------------------- people


def _email(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if EMAIL.fullmatch(value) else None


def _domain(email):
    return email.rsplit("@", 1)[1] if email else None


def _direct(row):
    referrer = row.get("referrer")
    return (not referrer or referrer == "$direct") and not row.get("utm_source")


def people(census_rows, prior_rows, c, m):
    """Everyone in the census with a tier, flags and a score, highest score first."""
    before = {row["person_id"]: row for row in prior_rows}
    internal = set(c["internal_people"]) | set(m["internal"]["people"])
    domains = set(c["exclude_email_domains"]) | set(m["internal"]["email_domains"])
    signup, activation = set(m["key_events"]["signup"]), set(m["key_events"]["activation"])
    intent = (
        set(m["key_events"]["intent"])
        | set(m["key_events"]["booking"])
        | set(m["key_events"]["payment"])
    )
    failure = set(m["key_events"]["failure"])
    result = []
    for row in census_rows:
        email = _email(row.get("email"))
        events = [e for e in row.get("key_events") or [] if isinstance(e, str)]
        earlier = before.get(row["person_id"]) or {}
        flags = [
            flag
            for flag, on in (
                ("rage_click", row.get("rage_clicks")),
                ("dead_click", row.get("dead_clicks")),
                ("error", row.get("errors")),
                ("failure", failure & set(events)),
                ("intent", intent & set(events)),
                ("activated", activation & set(events)),
                ("returning", earlier.get("prior_sessions")),
            )
            if on
        ]
        team = row["person_id"] in internal or (
            email and (email in internal or _domain(email) in domains)
        )
        single = (row.get("pageviews") or 0) <= 1 and not (set(events) - {"$exception"})
        country = (row.get("country") or "").lower()
        browser = (row.get("browser") or "").lower()
        if team:
            tier = "team"
        elif not email and not row.get("pageviews") and set(events) - {"$exception"}:
            # A product account seen only through server events: not a website visitor.
            tier = "account"
        elif (
            not email
            and single
            and _direct(row)
            and (
                country in m["bots"]["countries"] or browser in m["bots"]["browsers"] or not browser
            )
        ):
            tier = "bot"
        elif email and signup & set(events):
            tier = "new_user"
        elif email and intent & set(events):
            tier = "lead"
        elif email:
            tier = "user"
        elif single:
            tier = "passer_by"
        else:
            tier = "browser"
        score = {"new_user": 50, "lead": 45, "user": 10, "browser": 0, "account": 0}.get(tier, -100)
        score += 30 * bool({"rage_click", "error", "failure"} & set(flags))
        score += 20 * ("intent" in flags) + 5 * ("returning" in flags)
        score += min(row.get("paths") or 0, 10) * 2 + min(row.get("pageviews") or 0, 30)
        result.append(
            {
                **row,
                "email": email,
                "name": (row.get("name") or "").strip()[:200]
                if isinstance(row.get("name"), str)
                else "",
                "key_events": events,
                "tier": tier,
                "flags": flags,
                "score": score,
                "prior_sessions": earlier.get("prior_sessions") or 0,
                "prior_days": earlier.get("prior_days") or 0,
                "first_ever": earlier.get("first_ever") or row.get("first_seen"),
            }
        )
    result.sort(key=lambda p: (-p["score"], p["person_id"]))
    return result


def counts(p):
    by = {tier: 0 for tier in TIERS}
    for person in p:
        by[person["tier"]] += 1
    return {"people": len(p), "identified": sum(1 for x in p if x["email"]), **by}


def choose(p, n):
    """Who gets a journey read: new users and failures, the most engaged anonymous browsers,
    then by score. Server-only accounts never: their failures are in `frictions`."""
    eligible = [x for x in p if x["tier"] in ("new_user", "lead", "user", "browser")]
    must = [
        x
        for x in eligible
        if x["tier"] == "new_user" or {"failure", "rage_click", "error"} & set(x["flags"])
    ]
    browsers = [x for x in eligible if x["tier"] == "browser" and x not in must]
    must += browsers[:ANONYMOUS_JOURNEYS]
    rest = [x for x in eligible if x not in must]
    picked = (must + rest)[: max(n, min(len(must), MAX_TIMELINE_PEOPLE * 2))]
    return [x["person_id"] for x in picked]


def batches(ids):
    return [ids[i : i + MAX_TIMELINE_PEOPLE] for i in range(0, len(ids), MAX_TIMELINE_PEOPLE)]


def stalls(p, m):
    """People who reached a step and not the next, per step, from their key events."""
    steps = m["steps"]
    found = []
    if len(steps) < 2:
        return found
    for person in p:
        if person["tier"] in ("team", "bot"):
            continue
        reached = [
            i
            for i, step in enumerate(steps)
            if step in person["key_events"] or (step == "$pageview" and person.get("pageviews"))
        ]
        if not reached:
            continue
        last = max(reached)
        # Visiting and not starting is the conversion rate, not a friction.
        if last < len(steps) - 1 and steps[last] != "$pageview":
            found.append(
                {"person_id": person["person_id"], "after": steps[last], "missing": steps[last + 1]}
            )
    return found


def site_paths(rows, has_more=False, limit=8):
    """Where visitors came in, went and left, from `paths`. Sessions, not people; `known` have
    an email. `exits` count sessions of 2+ pages that never reached intent or signup."""
    landing, exits, routes = {}, {}, []
    total = {"sessions": 0, "known": 0, "bounced": 0, "intent": 0, "signup": 0, "rage": 0}
    for row in rows:
        n = int(row.get("sessions") or 0)
        bounced = int(row.get("bounced") or 0)
        for key in total:
            total[key] += n if key == "sessions" else int(row.get(key) or 0)
        entry = str(row.get("entry") or "(none)")[:200]
        page = landing.setdefault(entry, {"path": entry, "sessions": 0, "bounced": 0})
        page["sessions"] += n
        page["bounced"] += bounced
        left = n - max(int(row.get("intent") or 0), int(row.get("signup") or 0))
        if " > " in str(row.get("route") or "") and left > 0:
            exit_ = str(row.get("exit") or "(none)")[:200]
            exits[exit_] = exits.get(exit_, 0) + left
        if " > " in str(row.get("route") or "") and n >= 2:
            reached = "signup" if row.get("signup") else "intent" if row.get("intent") else None
            routes.append(
                {
                    "route": str(row["route"])[:400],
                    "sessions": n,
                    "people": int(row.get("people") or 0),
                    "reached": reached,
                }
            )
    top = sorted(landing.values(), key=lambda x: (-x["sessions"], x["path"]))[:limit]
    return {
        **total,
        "landing": top,
        "exits": [
            {"path": k, "sessions": v}
            for k, v in sorted(exits.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        ],
        "routes": sorted(routes, key=lambda x: (-x["sessions"], x["route"]))[:limit],
        "truncated": bool(has_more),
    }


def replay(m, session_id, recorded):
    """The replay link of a session in `recordings()`, else None."""
    if (
        not m.get("replay_url")
        or not isinstance(session_id, str)
        or not SESSION.fullmatch(session_id)
        or session_id not in (recorded or ())
    ):
        return None
    return m["replay_url"] + session_id


def _step(label, at=None, session=None):
    return {"label": label, "at": at, "last": at, "times": 1, "session": session}


def journey(rows, m, recorded=frozenset(), limit=24):
    """A person's timeline for reading: pages, labelled clicks, rage and dead clicks, errors and
    key events in order, repeats folded; other events only count in `other`. Skipped events
    show as one step, so it still ends where they stopped. `replays`: recorded sessions."""
    keys = set(key_event_names(m))
    steps, other, seen = [], 0, None
    for row in rows:
        n = row.get("n")
        if isinstance(n, int) and isinstance(seen, int) and n > seen + 1:
            steps.append(_step(f"... {n - seen - 1} events in between not read"))
        seen = n if isinstance(n, int) else seen
        event, path = row["event"], row.get("path") or ""
        element = (row.get("element") or "").strip()
        if event == "$pageview":
            label = f"opened {path or '(unknown page)'}"
        elif event == "$autocapture":
            if not element:
                other += 1
                continue
            label = f"clicked {element} on {path}"
        elif event == "$rageclick":
            label = f"rage-clicked {element + ' ' if element else ''}on {path}"
        elif event == "$dead_click":
            label = f"clicked {element + ' ' if element else ''}on {path}, nothing happened"
        elif event == "$exception":
            label = f"hit an error on {path}: {row.get('message') or 'unknown'}"
        elif event in keys:
            label = f"{event} ({', '.join(kind_of(m, event))})"
            if row.get("message"):
                label += f": {row['message']}"
        else:
            other += 1
            continue
        if steps and steps[-1]["label"] == label:
            steps[-1]["times"] += 1
            steps[-1]["last"] = row["timestamp"]
            continue
        steps.append(_step(label, row["timestamp"], row.get("session")))
    if len(steps) > limit:
        cut = len(steps) - limit
        steps = steps[: limit // 2] + [_step(f"... {cut} more steps")] + steps[-(limit // 2) :]
    sessions = [s for s in dict.fromkeys(r.get("session") for r in rows) if s]
    return {
        "steps": steps,
        "other": other,
        "sessions": sessions,
        "replays": [u for u in (replay(m, s, recorded) for s in sessions) if u][:3],
    }


# ---------------------------------------------------------------- frictions and the ledger


def _friction_id(kind, page, detail):
    raw = json.dumps([kind, page or "", (detail or "")[:80]])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def frictions(rows, loops, stalled, p):
    """Group raw signals into frictions with the people behind them, most people first."""
    by_id = {x["person_id"]: x for x in p}
    real = {x["person_id"] for x in p if x["tier"] not in ("team", "bot")}
    kinds = {"$rageclick": "rage_click", "$dead_click": "dead_click", "$exception": "error"}
    found = {}

    def add(kind, page, detail, row, n):
        ids = [i for i in row.get("person_ids") or [] if i in real]
        if not ids:
            return
        first, last = row.get("first_at"), row.get("last_at")
        fid = _friction_id(kind, page, detail)
        item = found.setdefault(
            fid,
            {
                "id": fid,
                "kind": kind,
                "page": page or "",
                "detail": (detail or "")[:160],
                "person_ids": [],
                "session_ids": [],
                "count": 0,
                "first_at": first,
                "last_at": last,
            },
        )
        item["person_ids"] = list(dict.fromkeys(item["person_ids"] + ids))
        sessions = [s for s in row.get("session_ids") or [] if s]
        item["session_ids"] = list(dict.fromkeys(item["session_ids"] + sessions))[:5]
        item["count"] += n
        item["first_at"] = min(filter(None, [item["first_at"], first]), default=None)
        item["last_at"] = max(filter(None, [item["last_at"], last]), default=None)

    for row in rows:
        kind = kinds.get(row["event"], "failure")
        detail = row.get("message") or row.get("element") or ""
        if kind == "failure":
            detail = f"{row['event']}: {detail}" if detail else row["event"]
        add(kind, row.get("path"), detail, row, row.get("n") or 0)
    for row in loops:
        opened = f"opened {row.get('most')} times in one session"
        add("loop", row.get("path"), opened, row, row.get("sessions") or 0)
    grouped = {}
    for s in stalled:
        grouped.setdefault((s["after"], s["missing"]), []).append(s["person_id"])
    for (after, missing), ids in grouped.items():
        add("stall", "", f"reached {after}, never {missing}", {"person_ids": ids}, len(ids))
    result = []
    for item in found.values():
        people_named = [by_id[i] for i in item["person_ids"] if i in by_id]
        item["people"] = len(item["person_ids"])
        item["identified"] = [x["email"] for x in people_named if x["email"]][:10]
        # The product failing, or a new user hitting a click or error on their way in, is
        # enough on its own; anything else needs two people. A loop always needs two.
        item["blocking"] = item["kind"] in ("failure", "stall") or (
            item["kind"] in ("rage_click", "dead_click", "error")
            and any(x["tier"] == "new_user" for x in people_named)
        )
        item["reportable"] = item["people"] >= 2 or item["blocking"]
        result.append(item)
    result.sort(key=lambda f: (-f["reportable"], -f["people"], -f["count"], f["id"]))
    return result[: MAX_FRICTIONS * 2]


def previous(text):
    """The map, ledger and brief of the newest earlier report. Its evidence is data only."""
    empty = dict.fromkeys(("map", "window", "site", "brief"))
    empty["ledger"] = {"frictions": [], "people": []}
    if not text:
        return empty
    try:
        evidence = read_evidence(text)
    except ContractError:
        return {**empty, "unreadable": True}
    ledger = evidence.get("ledger") or {}
    try:
        m = check_map(evidence.get("map")) if evidence.get("map") else None
    except ContractError:
        m = None
    return {
        "map": m,
        "ledger": {
            "frictions": list(ledger.get("frictions") or []),
            "people": list(ledger.get("people") or []),
        },
        "window": evidence.get("window"),
        "site": evidence.get("site") if isinstance(evidence.get("site"), dict) else None,
        "brief": evidence.get("brief") if isinstance(evidence.get("brief"), dict) else None,
    }


def carry(old, p, f, c):
    """The ledger after this run: frictions new, ongoing or fixed; named people with when they
    were first and last surfaced. Entries older than 90 days drop."""
    today = c["today"]
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=LEDGER_DAYS)).strftime(
        "%Y-%m-%d"
    )
    before = {item.get("id"): item for item in old.get("frictions") or [] if isinstance(item, dict)}
    frictions_out = []
    now_ids = set()
    for item in f:
        if not item["reportable"]:
            continue
        now_ids.add(item["id"])
        earlier = before.get(item["id"])
        days = sorted(set((earlier or {}).get("days") or []) | {today})[-30:]
        frictions_out.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "page": item["page"],
                "detail": item["detail"],
                "status": "ongoing" if earlier and earlier.get("status") != "fixed" else "new",
                "first_seen": (earlier or {}).get("first_seen") or today,
                "last_seen": today,
                "days": days,
                "people": item["people"],
            }
        )
    for fid, item in before.items():
        if fid in now_ids or not isinstance(item, dict) or (item.get("last_seen") or "") < cutoff:
            continue
        gone = (
            datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(item["last_seen"], "%Y-%m-%d")
        ).days
        fixed = item.get("status") == "fixed" or (
            gone >= FIXED_AFTER_DAYS and len(item.get("days") or []) >= 2
        )
        frictions_out.append(
            {**item, "status": "fixed" if fixed else item.get("status", "ongoing")}
        )
    seen = {item.get("email"): item for item in old.get("people") or [] if isinstance(item, dict)}
    people_out = []
    for person in p:
        if person["tier"] not in NAMED_TIERS:
            continue
        earlier = seen.pop(person["email"], None) or {}
        people_out.append(
            {
                "email": person["email"],
                "tier": person["tier"],
                "first_surfaced": earlier.get("first_surfaced") or today,
                "last_surfaced": today,
                "came_back": bool(earlier) and earlier.get("last_surfaced") != today,
            }
        )
    people_out += [item for item in seen.values() if (item.get("last_surfaced") or "") >= cutoff]
    return {"frictions": frictions_out[:200], "people": people_out[:500]}


# ---------------------------------------------------------------- evidence and the check


def profile(company="", role="", about="", linkedin_url=None, company_url=None, basis=""):
    """What the public web says about a new person's work. `basis` says what ties a role or
    profile to them, such as their email's domain; leave out anything nothing ties."""
    for url, pattern in ((linkedin_url, LINKEDIN), (company_url, SITE)):
        if url is not None and not pattern.fullmatch(url):
            raise ContractError(f"not a LinkedIn profile or https site: {str(url)[:80]}")
    w = {"company": _line(company, 100), "role": _line(role, 100), "about": _line(about, 200)}
    w |= {"linkedin_url": linkedin_url, "company_url": company_url, "basis": _line(basis, 160)}
    if (w["role"] or linkedin_url) and not w["basis"]:
        raise ContractError("a role or LinkedIn profile needs the basis that ties it to them")
    return w if any(w.values()) else None


def named(person, summary, action="", stopped_at="", replay_url=None, work=None):
    """One person's row for the evidence and for People. Only identified tiers. New: signed up,
    or gave an email for the first time, in the window."""
    if person["tier"] not in NAMED_TIERS or not person["email"]:
        raise ContractError("only identified people are named")
    return {
        "email": person["email"],
        "name": person["name"] or "",
        "tier": person["tier"],
        "summary": " ".join(summary.split())[:SUMMARY_LIMIT],
        "stopped_at": " ".join((stopped_at or "").split())[:200],
        "action": " ".join((action or "").split())[:ACTION_LIMIT],
        "first_seen": str(person.get("first_seen") or "")[:40],
        "last_seen": str(person.get("last_seen") or "")[:40],
        "source": str(person.get("utm_source") or person.get("referrer") or "")[:100],
        "replay_url": replay_url,
        "new": person["tier"] == "new_user"
        or (person["tier"] == "lead" and not person.get("prior_sessions")),
        "work": work,
    }


def _line(value, limit=PERIODS["week"]["line"]):
    return " ".join(str(value or "").split())[:limit]


def _group(value):
    label, emails, *note = value
    return {
        "label": _line(label, GROUP_LABEL),
        "emails": [str(e).strip().lower() for e in emails],
        "note": _line(note[0] if note else "", GROUP_NOTE),
    }


def brief(observations, new=(), stages=(), stuck=(), returning=()):
    """The founder's brief under the Status line. `observations`: [(text, friction_id or
    None)]. `new`: [(email, line)], the few new people worth knowing on their own. People are
    grouped, each group (label, [emails]) or (label, [emails], note): `stages`, every new person
    by how far they got; `stuck`, who hit each problem; `returning`, what returning users did.
    assemble() keeps it to the period's limits and adds the table, names, work and links."""
    return {
        **{
            name: [_group(g) for g in groups]
            for name, groups in zip(GROUPS, (stages, stuck, returning), strict=True)
        },
        "new": [{"email": str(e).strip().lower(), "line": _line(x)} for e, x in new],
        "observations": [
            {"text": _line(text), "friction": friction or None} for text, friction in observations
        ],
    }


def _number(value, tracked=True):
    if value is None:
        return "not tracked" if not tracked else "no earlier data"
    return f"{value:.1f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _brief_person(v):
    """One person in the brief: who, their work when known, what they did, then links."""
    w = v.get("work") or {}
    who = f"**{v['name']}** ({v['email']})" if v["name"] else f"**{v['email']}**"
    job = "; ".join(
        filter(None, (" at ".join(filter(None, (w.get("role"), w.get("company")))), w.get("about")))
    )
    links = (
        ("LinkedIn", w.get("linkedin_url")),
        (w.get("company") or "Site", w.get("company_url")),
        ("Replay", v["replay_url"]),
    )
    links = " · ".join(f"[{k}]({u})" for k, u in links if u)
    return f"- {who}: " + " ".join(filter(None, (job and job.rstrip(".") + ".", v["line"], links)))


def _short(work):
    """Who someone is in a group: "Co-founder, Byro"."""
    w = work or {}
    return ", ".join(filter(None, (w.get("role"), w.get("company"))))


def _group_line(g, count=False):
    """A group in one line. Only a stage counts its people; a problem's label says how often."""
    names = ", ".join(
        (x["name"] or x["email"]) + (f" ({x['company']})" if x["company"] else "")
        for x in g["people"]
    )
    note = g["note"]
    note = f". {note[0].upper()}{note[1:]}" if note else ""
    return f"- {g['label']}" + (f" ({len(g['people'])})" if count else "") + f": {names}{note}"


def average_row(period, days=None):
    """The table's second row. It names only the days or weeks the average covers, fewer
    when tracking began within them."""
    if not days or days >= (7 if period == "day" else 28):
        return ROWS[period][1]
    if period == "day":
        return "Yesterday only" if days == 1 else f"Last {days} days, daily average"
    return "The week before" if days == 7 else f"The {days // 7} weeks before, weekly average"


def render_brief(b):
    """The brief as Markdown, exactly as it goes under the Status line."""
    fn, period = b["funnel"], b.get("period", "day")
    if fn is None:
        lines = [f"Visitors were not measured {'this week' if period == 'week' else 'today'}."]
    else:
        labels = [x["label"] for x in fn["steps"]]
        lines = ["| | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)]
        current, average = ROWS[period][0], average_row(period, fn.get("days"))
        lines.append(
            f"| {current} | "
            + " | ".join(_number(x["yesterday"], False) for x in fn["steps"])
            + " |"
        )
        lines.append(
            f"| {average} | "
            + " | ".join(_number(x["average"], x["yesterday"] is not None) for x in fn["steps"])
            + " |"
        )
    new = sum(len(g["people"]) for g in b["stages"])
    if new:
        lines += ["", f"**New users: {new}, by how far they got**", ""]
        lines += [_group_line(g, count=True) for g in b["stages"]]
    else:
        lines += ["", "**New users**", "", "- Nobody new signed up or left an email."]
    if b["new"]:
        lines += ["", "**Worth knowing**", ""] + [_brief_person(v) for v in b["new"]]
    if b["stuck"]:
        lines += ["", "**Where people got stuck**", ""] + [_group_line(g) for g in b["stuck"]]
    if b["returning"]:
        n = sum(len(g["people"]) for g in b["returning"])
        lines += ["", f"**Returning users ({n})**", ""] + [_group_line(g) for g in b["returning"]]
    lines += ["", f"**{ALSO[period]}**", ""]
    lines += [f"- {o['text']}" for o in b["observations"]] or ["- Nothing else stood out."]
    return "\n".join(lines)


def assemble(
    c,
    status,
    reason,
    requests,
    p,
    named_people,
    f,
    ledger,
    m,
    limitations=(),
    site=None,
    brief=None,
    funnel=None,
    previous=None,
):
    evidence = _assemble(
        c, status, reason, requests, p, named_people, f, ledger, m, limitations, site
    )
    if not c["look_up_new_people"] and any(row.get("work") for row in named_people):
        raise ContractError("the founder turned off looking up new people; leave out work")
    if brief is not None:
        if funnel is not None and funnel.get("period", "day") != c["period"]:
            raise ContractError('pass c["period"] to funnel()')
        limit = PERIODS[c["period"]]
        groups = {k: (brief.get(k) or [])[:MAX_GROUPS] for k in GROUPS}
        brief = {
            k: [{**x, f: _line(x[f], limit["line"])} for x in brief.get(k) or []][: limit[k]]
            for k, f in (("new", "line"), ("observations", "text"))
        }
        named_rows = {row["email"]: row for row in named_people}
        out_groups = {k: [] for k in GROUPS}
        for k, kind in groups.items():
            listed = set()
            for g in kind:
                people = []
                for email in g["emails"]:
                    row = named_rows.get(email)
                    if row is None:
                        raise ContractError(f"{email} is in the brief but is not a named person")
                    if k == "stages" and not row.get("new"):
                        raise ContractError(f"{email} is not new; put them under returning")
                    if k == "returning" and row.get("new"):
                        raise ContractError(f"{email} is new; put them in a stage")
                    if email in listed and k != "stuck":
                        raise ContractError(f"{email} belongs to one {k} group only")
                    listed.add(email)
                    people.append(
                        {"email": email, "name": row["name"], "company": _short(row.get("work"))}
                    )
                out_groups[k].append({"label": g["label"], "note": g["note"], "people": people})
        new_people = {e for e, row in named_rows.items() if row.get("new")}
        staged = {x["email"] for g in out_groups["stages"] for x in g["people"]}
        if new_people - staged:
            raise ContractError(
                "put every new person in one of the stages; missing "
                + ", ".join(sorted(new_people - staged))
            )
        new = []
        for v in brief["new"]:
            row = named_rows.get(v["email"]) or {}
            if not row.get("new"):
                kind = "new person; put them under returning" if row else "person"
                raise ContractError(f"{v['email']} is in the brief but is not a named {kind}")
            new.append({**v, "name": row["name"], "replay_url": row["replay_url"]})
            new[-1]["work"] = row.get("work")
        _only_new({**brief, **out_groups}, named_rows, previous, c["period"])
        evidence["brief"] = {
            **brief,
            "new": new,
            **out_groups,
            "funnel": funnel,
            "period": c["period"],
        }
    return evidence


def _moment(value):
    """A census or window time as an aware datetime, or None when it cannot be read."""
    text = str(value or "").strip().replace(" ", "T").removesuffix("Z")
    try:
        moment = datetime.fromisoformat(text[:26])
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _said(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _only_new(b, named_rows, previous, period):
    """Only what is new since the last brief of the same period: not someone it showed who has
    done nothing since its window, nor an observation it already made. A week is never cut
    by a daily brief."""
    last = (previous or {}).get("brief") or {}
    end = _moment(((previous or {}).get("window") or {}).get("end"))
    if not last or end is None or last.get("period", "day") != period:
        return
    # A brief from before 1.6.0 listed visitors one per line.
    were = (last.get("visitors") or []) + (last.get("new") or [])
    for k in ("stuck", "returning"):
        were += [x for g in last.get(k) or [] if isinstance(g, dict) for x in g.get("people") or []]
    shown = {v.get("email") for v in were if isinstance(v, dict)}
    now = [x for k in ("stuck", "returning") for g in b.get(k) or [] for x in g["people"]]
    for v in b["new"] + now:
        seen = _moment(named_rows[v["email"]].get("last_seen"))
        if v["email"] in shown and seen is not None and seen <= end:
            raise ContractError(
                f"{v['email']} was in the last brief and has done nothing since it; leave them out"
            )
    said = {_said(o.get("text")) for o in last.get("observations") or [] if isinstance(o, dict)}
    for o in b["observations"]:
        if _said(o["text"]) in said:
            raise ContractError(
                "the last brief already said '" + o["text"][:80] + "'; leave it out, or say "
                "what changed since"
            )


def _assemble(c, status, reason, requests, p, named_people, f, ledger, m, limitations, site):
    known = {y["id"]: y["status"] for y in ledger["frictions"]}
    shown = ("id", "kind", "page", "detail", "people")
    return {
        "version": VERSION,
        "status": status,
        "status_reason": reason,
        "provider": "analytics.posthog",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": c["start"], "end": c["end"]},
        "requests": requests,
        "counts": counts(p),
        "people": named_people,
        "frictions": [
            {**{k: x[k] for k in shown}, "status": known.get(x["id"], "new")}
            for x in f
            if x["reportable"]
        ][:MAX_FRICTIONS],
        "ledger": ledger,
        "map": m,
        "site": site,
        "limitations": [str(x)[:300] for x in limitations][:20],
    }


def render_evidence(evidence):
    return (
        "\n\n"
        + EVIDENCE_MARKER
        + "\n```json\n"
        + json.dumps(evidence, ensure_ascii=False, indent=1)
        + "\n```\n"
    )


def read_evidence(text):
    if text.count(EVIDENCE_MARKER) != 1:
        raise ContractError("the report must end with one evidence block")
    tail = text.split(EVIDENCE_MARKER, 1)[1]
    match = re.fullmatch(r"\s*```json\n(.+?)\n```\s*", tail, re.S)
    if not match:
        raise ContractError("the evidence must be one fenced JSON block at the end")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ContractError(f"the evidence is not JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ContractError("the evidence must be an object")
    return value


def check(text):
    """The checks Tin runs before publishing, and a few more. Fix what this raises."""
    if len(text.encode()) > 240000:
        raise ContractError("the report is over 240000 bytes; shorten journeys")
    head = text.split(EVIDENCE_MARKER, 1)[0]
    statuses = re.findall(r"^Status: (.*?)\s*$", head, re.M)
    if len(statuses) != 1 or statuses[0] not in STATUSES:
        raise ContractError("the report needs one 'Status: <status>' line")
    e = read_evidence(text)
    keys = "version status status_reason provider window requests counts people frictions ledger"
    for key in (keys + " map limitations").split():
        if key not in e:
            raise ContractError(f"the evidence is missing {key}")
    if (
        e["version"] != VERSION
        or e["provider"] != "analytics.posthog"
        or e["status"] != statuses[0]
    ):
        raise ContractError("the evidence does not match the report's Status line")
    reason = e["status_reason"]
    if (
        not isinstance(reason, str)
        or len(reason) > REASON_LIMIT
        or "\n" in reason
        or ((e["status"] == "complete") == bool(reason.strip()))
    ):
        raise ContractError("status_reason is empty exactly when the status is complete")
    for item in e["requests"]:
        if item.get("outcome") not in REQUEST_OUTCOMES or item.get("operation") not in OPERATIONS:
            raise ContractError("each request needs a known operation and outcome")
    if e["status"] == "nothing to report" and not any(
        r["outcome"] == "ok" and r["operation"] == "query.hogql" for r in e["requests"]
    ):
        raise ContractError("nothing to report needs at least one answered query")
    if len(e["people"]) > MAX_NAMED:
        raise ContractError(f"name at most {MAX_NAMED} people")
    for row in e["people"]:
        if row.get("tier") not in NAMED_TIERS or not _email(row.get("email")):
            raise ContractError("each named person needs an email and an identified tier")
        if not row.get("summary") or len(row["summary"]) > SUMMARY_LIMIT:
            raise ContractError(
                f"each named person needs a summary of at most {SUMMARY_LIMIT} characters"
            )
        if row.get("replay_url") is not None and not str(row["replay_url"]).startswith(
            str(e["map"].get("replay_url") or "\0")
        ):
            raise ContractError("replay links come from replay(), never written by hand")
        if row["email"] not in head:
            raise ContractError(f"{row['email']} is in the evidence but not in the report")
    for item in e["frictions"]:
        if item.get("kind") not in FRICTION_KINDS or item.get("status") not in FRICTION_STATUSES:
            raise ContractError("each friction needs a known kind and status")
    check_map(e["map"])
    if e["status"] != "invalid configuration":
        _check_brief(e, head)
    return e


# A claim that something is broken needs a recorded failure behind it, or a "may".
BROKEN_WORDS = re.compile(r"\b(bug|broken|breaks|fails|failing)\b", re.IGNORECASE)


def _check_brief(e, head):
    b = e.get("brief")
    if not isinstance(b, dict):
        raise ContractError("the report needs a brief: build it with brief() and assemble()")
    fn = b.get("funnel")
    answered = any(r.get("step") == "funnel" and r.get("outcome") == "ok" for r in e["requests"])
    if fn is None and answered:
        raise ContractError("pass funnel() to assemble(); the funnel query answered")
    if fn is not None:
        if not answered:
            raise ContractError("the table needs an answered funnel query")
        if [x.get("label") for x in fn.get("steps") or []] != [
            x["label"] for x in e["map"].get("funnel") or []
        ]:
            raise ContractError("the table's steps must be the map's funnel")
    limit = PERIODS.get(b.get("period", "day"))
    if limit is None or (fn is not None and fn.get("period", "day") != b.get("period", "day")):
        raise ContractError("the brief and its table need the run's period")
    for part in ("new", "observations"):
        if len(b.get(part) or []) > limit[part]:
            raise ContractError(f"keep the brief to {limit[part]} {part}")
    groups = [g for k in GROUPS for g in (b.get(k) or [])]
    if any(len(b.get(k) or []) > MAX_GROUPS for k in GROUPS):
        raise ContractError(f"the brief keeps to {MAX_GROUPS} groups of each kind")
    named_emails = {row["email"] for row in e["people"]}
    for v in (b.get("new") or []) + [x for g in groups for x in g.get("people") or []]:
        if v.get("email") not in named_emails:
            raise ContractError(f"{v.get('email')} is in the brief but is not a named person")
    observations = b.get("observations") or []
    recorded = {x["id"] for x in e["frictions"] if x["kind"] in ("failure", "error")}
    for o in observations:
        if (
            BROKEN_WORDS.search(o["text"])
            and o.get("friction") not in recorded
            and not re.search(r"\bmay\b", o["text"], re.IGNORECASE)
        ):
            raise ContractError(
                "an observation that something is broken needs a recorded failure or error "
                "as its friction, or say 'there may be a bug'"
            )
    lines = [v["line"] for v in b.get("new") or []]
    lines += [o["text"] for o in observations] + [g["label"] + " " + g["note"] for g in groups]
    text = " ".join(lines)
    found = INTERNAL_WORDS.search(text)
    if found:
        raise ContractError(f"the brief uses '{found.group(0)}'; write it in the founder's words")
    rendered = render_brief(b)
    at = head.find(rendered)
    status_at = re.search(r"^Status: ", head, re.M).start()
    first_section = head.find("\n## ")
    if at < status_at or (first_section != -1 and at > first_section):
        raise ContractError("put render_brief() under the Status line, before the first section")
```
