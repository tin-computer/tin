# Deterministic analytics resources

Execute this reviewed Python block after STATISTICS.md. All SQL comes from fixed builders.

```python
import datetime as dt
import hashlib
import json
import math
import re
import statistics

VERSION = "reusable-v2"
KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,63}\Z")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\Z")
RESERVED = {"Unknown", "Ambiguous", "Other"}
NO_PERSON = "00000000-0000-0000-0000-000000000000"
EVIDENCE_MARKER = "<!-- tin-analytics-evidence-v1 -->"
STATUSES = (
    "complete",
    "incomplete",
    "invalid configuration",
    "unsupported exclusions",
    "schema changed",
)
# Fixed traffic channels, tested in this order on the lowercased referring domain without a
# leading "www.". UTM mediums decide paid, email and social before any referrer is read.
CHANNELS = ("Paid", "Email", "Social", "Direct", "Internal", "AI assistants", "Search", "Referral")
WWW = r"^www\."
PAID_MEDIUM = r"^(?:cpc|ppc|cpm|cpv|cpa|paid.*|display|retargeting|ads?)$"
EMAIL_MEDIUM = r"^(?:e-?mail|newsletter)$"
SOCIAL_MEDIUM = r"^(?:social|social[-_ ]?media|organic[-_ ]?social|sm)$"
AI_DOMAINS = (
    r"(?:^|\.)(?:chatgpt\.com|chat\.openai\.com|perplexity\.ai|claude\.ai"
    r"|gemini\.google\.com|copilot\.microsoft\.com)$"
)
SEARCH_DOMAINS = (
    r"^(?:(?:search|m)\.)?(?:google|bing|duckduckgo|yahoo|yandex|baidu|ecosia|naver|qwant"
    r"|startpage|seznam)\.[a-z]{2,3}(?:\.[a-z]{2})?$|^search\.brave\.com$"
)
SOCIAL_DOMAINS = (
    r"(?:^|\.)(?:facebook\.com|fb\.com|instagram\.com|t\.co|twitter\.com|x\.com|linkedin\.com"
    r"|lnkd\.in|reddit\.com|youtube\.com|youtu\.be|tiktok\.com|pinterest\.com|threads\.net"
    r"|bsky\.app|mastodon\.social|news\.ycombinator\.com)$"
)


def literal(value):
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int and abs(value) <= 10**12:
        return str(value)
    if type(value) is str and 1 <= len(value) <= 256 and not any(ord(c) < 32 for c in value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    raise ValueError("unsupported scalar literal")


def regex(expression):
    """A builder-owned regular expression as a HogQL string literal (never user text)."""
    if (
        type(expression) is not str
        or not 1 <= len(expression) <= 1000
        or any(ord(c) < 32 for c in expression)
    ):
        raise ValueError("unsupported pattern")
    return "'" + expression.replace("\\", "\\\\").replace("'", "\\'") + "'"


def prop(name, source="properties"):
    if type(name) is not str or not KEY.fullmatch(name):
        raise ValueError("unsupported property")
    if source not in ("properties", "person.properties"):
        raise ValueError("unsupported property source")
    raw = f"JSONExtractRaw({source},{literal(name)})"
    return f"if({raw}='' OR {raw}='null',NULL,if(startsWith({raw},'\"'),JSONExtractString({source},{literal(name)}),{raw}))"


def identity(key):
    if key == "distinct_id":
        return "distinct_id"
    if key == "person_id":
        # PostHog's person for the event. identify() merges anonymous and identified
        # distinct IDs into one person, so browser and server events of one person share it.
        # An empty or all-zero ID links nothing and counts as a missing key.
        return f"if(toString(person_id) IN ('',{literal(NO_PERSON)}),NULL,toString(person_id))"
    if type(key) is str and key.startswith("event:"):
        name = key[6:]
        prop(name)
        return f"if(startsWith(JSONExtractRaw(properties,{literal(name)}),'\"'),JSONExtractString(properties,{literal(name)}),NULL)"
    raise ValueError("identity must be distinct_id, person_id or event:<string property>")


def window_hours(key):
    """The N of a window:<N> chain key (1-168 hours), or None for an identity chain."""
    if type(key) is str and key.startswith("window:"):
        if not re.fullmatch(r"[1-9][0-9]{0,2}", key[7:]) or int(key[7:]) > 168:
            raise ValueError("window chains are window:<1-168 hours>")
        return int(key[7:])
    return None


def chain(key):
    # A window chain has no attempt key: each actor is one chain, bounded by the window.
    return "'window'" if window_hours(key) else identity(key)


def nonempty(expr):
    return f"(isNotNull({expr}) AND toString({expr})!='')"


def stable_hash(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def hostnames(values, limit, longest):
    return (
        type(values) is list
        and len(values) <= limit
        and all(type(h) is str and len(h) <= longest and HOST.fullmatch(h) for h in values)
        and len(values) == len(set(values))
    )


def validate_hosts(hosts):
    if not hostnames(hosts, 5, 253):
        raise ValueError("website_hosts must contain at most five distinct lowercase hostnames")


def validate_domains(domains):
    # Compiled into every query; the length cap keeps the largest plan inside 8000 bytes.
    if not hostnames(domains, 10, 63):
        raise ValueError(
            "exclude_email_domains must contain at most ten distinct lowercase hostnames"
        )


def exclusion_key(name):
    """An event property or person:<property> usable as an exclusion or flag field."""
    if type(name) is not str or name == "distinct_id":
        raise ValueError("invalid exclusion field")
    prop(name[7:] if name.startswith("person:") else name)
    return name


def settings(inputs, now=None):
    allowed = {
        "reporting_days",
        "as_of_utc",
        "event_mapping",
        "exclusions",
        "website_hosts",
        "exclude_email_domains",
        "internal_flag_property",
    }
    if not isinstance(inputs, dict) or set(inputs) - allowed:
        raise ValueError("unexpected client input")
    c = dict(
        reporting_days=7,
        as_of_utc="",
        event_mapping="",
        exclusions="",
        exclude_email_domains=[],
        internal_flag_property="",
    )
    c.update(inputs)
    hosts = c.get("website_hosts", [])
    validate_hosts(hosts)
    validate_domains(c["exclude_email_domains"])
    if type(c["internal_flag_property"]) is not str:
        raise ValueError("invalid internal_flag_property")
    if c["internal_flag_property"]:
        exclusion_key(c["internal_flag_property"])
    if type(c["reporting_days"]) is not int or not 1 <= c["reporting_days"] <= 31:
        raise ValueError("invalid reporting_days")
    for k in ("event_mapping", "exclusions"):
        if type(c[k]) is not str or len(c[k]) > 2000:
            raise ValueError("invalid " + k)
    anchor = c["as_of_utc"]
    if type(anchor) is not str:
        raise ValueError("invalid date")
    if anchor:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", anchor):
            raise ValueError("invalid date")
        end = dt.datetime.combine(dt.date.fromisoformat(anchor), dt.time(), dt.timezone.utc)
    else:
        clock = now or dt.datetime.now(dt.timezone.utc)
        if clock.tzinfo is None:
            raise ValueError("naive clock")
        end = clock.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    current = end - dt.timedelta(days=c["reporting_days"])
    prior = current - dt.timedelta(days=c["reporting_days"])
    # Date/window changes do not silently replace the semantic mapping. The PostHog project is
    # the one selected in Tin's Integrations; it is never an input or part of a request.
    binding = stable_hash({k: c[k] for k in ("event_mapping", "exclusions")})
    scoped = {
        "website_hosts": sorted(hosts),
        "exclude_email_domains": sorted(c["exclude_email_domains"]),
        "internal_flag_property": c["internal_flag_property"],
    }
    scoped = {k: v for k, v in scoped.items() if v}
    if scoped:
        binding = stable_hash({"base": binding, **scoped})
    return c, (prior, current, end), binding


def input_exclusions(c):
    """Rules the structured saved inputs compile to. Every plan carries them unchanged."""
    rules = []
    if c.get("exclude_email_domains"):
        rules.append(
            {
                "property": "person:email",
                "op": "suffix",
                "value": sorted(c["exclude_email_domains"]),
            }
        )
    if c.get("internal_flag_property"):
        rules.append({"property": c["internal_flag_property"], "op": "truthy", "value": True})
    return rules


def validate_plan(p):
    fields = {
        "version",
        "actor_key",
        "actor_label",
        "chain_key",
        "chain_label",
        "steps",
        "labels",
        "key_events",
        "error_events",
        "pageview_event",
        "path_property",
        "source_property",
        "paths",
        "category_property",
        "categories",
        "exclusions",
        "semantic_evidence",
        "traffic_actor_key",
        "traffic_chain_key",
    }
    if type(p) is dict and "_website_hosts" in p:
        validate_hosts(p["_website_hosts"])
        p = {k: v for k, v in p.items() if k != "_website_hosts"}
    if type(p) is not dict or set(p) != fields or p["version"] != VERSION:
        raise ValueError("unsupported plan shape/version")
    identity(p["actor_key"])
    chain(p["chain_key"])
    # Traffic attributes each session's entry, so its keys are identities, never a window.
    identity(p["traffic_actor_key"])
    identity(p["traffic_chain_key"])
    if p["actor_key"] == p["chain_key"]:
        raise ValueError("actor and chain keys must be distinct")
    for k in ("actor_label", "chain_label"):
        if type(p[k]) is not str or not 1 <= len(p[k]) <= 120:
            raise ValueError("missing unit")
    for k, lo, hi in (
        ("steps", 0, 6),
        ("key_events", 1, 4),
        ("error_events", 0, 2),
        ("paths", 0, 8),
        ("categories", 0, 8),
    ):
        a = p[k]
        if (
            type(a) is not list
            or not lo <= len(a) <= hi
            or any(type(x) is not str for x in a)
            or len(a) != len(set(a))
        ):
            raise ValueError("invalid " + k)
        for value in a:
            literal(value)
    if len(p["steps"]) == 1:
        raise ValueError("funnel requires two to six steps")
    if (
        type(p["labels"]) is not list
        or len(p["labels"]) != len(p["steps"])
        or any(type(x) is not str or not 1 <= len(x) <= 120 for x in p["labels"])
    ):
        raise ValueError("invalid labels")
    for k in ("paths", "categories"):
        for value in p[k]:
            safe_dimension(value)
    for k in ("category_property", "path_property", "source_property"):
        if p[k]:
            prop(p[k])
    if p["pageview_event"]:
        literal(p["pageview_event"])
    if bool(p["pageview_event"]) != bool(p["path_property"] and p["source_property"]):
        raise ValueError("partial traffic mapping")
    if p["category_property"] and re.search(
        r"email|phone|token|secret|password|(?:^|_)id$|name$", p["category_property"], re.I
    ):
        raise ValueError("identifying breakdown property")
    if p["category_property"] and not p["steps"]:
        raise ValueError("breakdown requires activation cohort")
    if (
        type(p["semantic_evidence"]) is not list
        or not 1 <= len(p["semantic_evidence"]) <= 12
        or any(type(x) is not str or not 1 <= len(x) <= 500 for x in p["semantic_evidence"])
    ):
        raise ValueError("missing semantic evidence")
    # A pageview step may use person/window identities while traffic keeps sessions:
    # coverage then reports the pageview under both (split_pageview).
    exclusions(p["exclusions"])
    return p


def split_pageview(p):
    funnel_keys = (p["actor_key"], p["chain_key"])
    traffic_keys = (p["traffic_actor_key"], p["traffic_chain_key"])
    return p["pageview_event"] in p["steps"] and funnel_keys != traffic_keys


def raw_field(name):
    exclusion_key(name)
    source = "person.properties" if name.startswith("person:") else "properties"
    key = name[7:] if name.startswith("person:") else name
    return f"JSONExtractRaw({source},{literal(key)})"


def exclusion_clause(rule):
    """One {property, op, value} rule. A row whose field is missing stays included."""
    if type(rule) is not dict or set(rule) != {"property", "op", "value"}:
        raise ValueError("invalid exclusion")
    name, op, value = rule["property"], rule["op"], rule["value"]
    if op == "eq":
        # Exact and type-preserving: true, "true" and 1 are three different values.
        literal(value)
        if name == "distinct_id":
            if type(value) is not str:
                raise ValueError("distinct_id exclusion must be text")
            return f"NOT coalesce(distinct_id={literal(value)},false)"
        expected = literal(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return f"NOT coalesce({raw_field(name)}={expected},false)"
    if op == "suffix":
        # The field ends in @domain or .domain (subdomains), case-insensitively.
        if not value:
            raise ValueError("suffix exclusions need one to ten domains")
        validate_domains(value)
        escaped = [d.replace(".", "\\.").replace("-", "\\-") for d in value]
        expression = "(?:^|[@.])(?:" + "|".join(escaped) + ")$"
        field = exclusion_field(exclusion_key(name))
        return f"NOT coalesce(match(lower(toString({field})),{regex(expression)}),false)"
    if op == "truthy":
        # A flag set as true, "true" (any case) or 1. False, 0 and missing stay included.
        if value is not True:
            raise ValueError("truthy exclusions take the value true")
        return f"NOT coalesce(lower({raw_field(name)}) IN ('true','\"true\"','1'),false)"
    raise ValueError("exclusion op must be eq, suffix or truthy")


def exclusions(items):
    # Six rules translated from the founder's prose plus the two structured inputs.
    if type(items) is not list or len(items) > 8:
        raise ValueError("invalid exclusions")
    return " AND ".join(exclusion_clause(x) for x in items) or "true"


def timestamp(x):
    if not isinstance(x, dt.datetime) or x.utcoffset() != dt.timedelta(0):
        raise ValueError("UTC boundary required")
    return "toDateTime('" + x.strftime("%Y-%m-%d %H:%M:%S") + "','UTC')"


def event_list(p):
    return sorted(
        set(
            p["steps"]
            + p["key_events"]
            + p["error_events"]
            + ([p["pageview_event"]] if p["pageview_event"] else [])
        )
    )


def where(p, start, end, events=None):
    s = f"timestamp>={timestamp(start)} AND timestamp<{timestamp(end)} AND ({exclusions(p['exclusions'])})"
    hosts = p.get("_website_hosts", [])
    validate_hosts(hosts)
    if hosts:
        # Scope every occurrence of pageviews, including inventory/coverage, consistently.
        # Server events have separate project-wide scope; never drop missing-host server rows.
        host = prop("$host")
        s += " AND (event!='$pageview' OR " + host + " IN (" + ",".join(map(literal, hosts)) + "))"
    if events is not None:
        if not events:
            raise ValueError("empty event selection")
        s += " AND event IN (" + ",".join(map(literal, events)) + ")"
    return s


def bucket(expr, values):
    allowed = ",".join(map(literal, values)) or "'__no_selected_category__'"
    return f"multiIf(NOT {nonempty(expr)},'Unknown',toString({expr}) IN ({allowed}),toString({expr}),'Other')"


def inventory(p, w):
    # Discovery is bounded; the catalog size does not limit exact selected-event queries.
    a, b, c = w
    hist = c - dt.timedelta(days=90)
    grouped = f"SELECT event,count() AS observed,countIf(timestamp>={timestamp(a)} AND timestamp<{timestamp(b)}) AS prior,countIf(timestamp>={timestamp(b)}) AS current,min(timestamp) AS first_seen,max(timestamp) AS last_seen FROM events WHERE {where(p, hist, c)} GROUP BY event"
    return f"SELECT *,count() OVER () AS total_event_types FROM ({grouped}) ORDER BY current+prior DESC,observed DESC,event LIMIT 200"


def coverage(p, w):
    a, b, c = w
    actor = funnel_actor = identity(p["actor_key"])
    chain_key = funnel_chain = chain(p["chain_key"])
    if p["pageview_event"]:
        ev = literal(p["pageview_event"])
        actor = f"if(event={ev},toString({identity(p['traffic_actor_key'])}),toString({actor}))"
        chain_key = (
            f"if(event={ev},toString({identity(p['traffic_chain_key'])}),toString({chain_key}))"
        )
    keys = sorted(
        set(
            [x["property"] for x in p["exclusions"]]
            + [x for x in (p["category_property"], p["path_property"], p["source_property"]) if x]
        )
    )
    # Each identity/property expression appears once, in the inner SELECT. A property is
    # missing when absent, JSON null or "": reading its raw JSON once keeps the query within
    # Tin's 8000-byte HogQL bound with eight exclusions and five website hosts.
    inner = [
        "event",
        f"if(timestamp<{timestamp(b)},'prior','current') AS period",
        "timestamp",
        f"toString({actor}) AS actor",
        f"toString({chain_key}) AS chain",
    ] + [
        f"{'distinct_id' if k == 'distinct_id' else raw_field(k)} AS f{i}"
        for i, k in enumerate(keys)
    ]
    has_actor = nonempty("actor")
    eligible = f"{has_actor} AND {nonempty('chain')}"
    fields = [
        "event",
        "period",
        "count() AS raw",
        f"countIf({has_actor}) AS actor_rows",
        f"countIf({eligible}) AS eligible_rows",
        f"uniqExactIf(actor,{has_actor}) AS actors",
        f"uniqExactIf(actor,{eligible}) AS eligible_actors",
        f"uniqExactIf(tuple(actor,chain),{eligible}) AS chains",
        "min(timestamp) AS first_seen",
        "max(timestamp) AS last_seen",
    ] + [
        f"countIf(f{i}='') AS missing_p{i}"
        if k == "distinct_id"
        else f"countIf(f{i} IN ('','null','\"\"')) AS missing_p{i}"
        for i, k in enumerate(keys)
    ]
    if split_pageview(p):
        # The pageview step's activation identity, beside its traffic identity above.
        inner += [f"toString({funnel_actor}) AS fa", f"toString({funnel_chain}) AS fc"]
        has_funnel_actor = nonempty("fa")
        fields += [
            f"uniqExactIf(fa,{has_funnel_actor}) AS funnel_actors",
            f"uniqExactIf(fa,{has_funnel_actor} AND {nonempty('fc')}) AS funnel_eligible_actors",
        ]
    rows = f"SELECT {','.join(inner)} FROM events WHERE {where(p, a, c, event_list(p))}"
    return (
        "SELECT "
        + ",".join(fields)
        + f" FROM ({rows}) GROUP BY event,period ORDER BY event,period LIMIT 41",
        keys,
    )


def trends(p, w):
    a, b, c = w
    hist = c - dt.timedelta(days=90)
    actor = identity(p["actor_key"])
    attempt = chain(p["chain_key"])
    selected = sorted(set(p["key_events"] + p["error_events"]))
    day = f"if(timestamp<{timestamp(a)},toString(toStartOfWeek(toTimeZone(timestamp,'UTC'),1)),toString(toDate(toTimeZone(timestamp,'UTC'))))"
    resolution = f"if(timestamp<{timestamp(a)},'week','day')"
    return f"SELECT {day} AS day,{resolution} AS resolution,event,count() AS raw,uniqExactIf(toString({actor}),{nonempty(actor)}) AS actors,uniqExactIf(tuple(toString({actor}),toString({attempt})),{nonempty(actor)} AND {nonempty(attempt)}) AS chains FROM events WHERE {where(p, hist, c, selected)} GROUP BY day,resolution,event ORDER BY day,resolution,event LIMIT 601"


def funnel_ctes(p, w, category=True):
    validate_plan(p)
    steps = p["steps"]
    n = len(steps)
    if not 2 <= n <= 6:
        raise ValueError("funnel unavailable")
    a, b, c = w
    actor = identity(p["actor_key"])
    hours = window_hours(p["chain_key"])
    attempt = chain(p["chain_key"])
    # Only the breakdown reads the category; the funnel query leaves it out to save bytes.
    cat = (
        bucket(prop(p["category_property"]), p["categories"])
        if p["category_property"] and category
        else "'Unknown'"
    )
    keyed = nonempty(actor) if hours else f"{nonempty(actor)} AND {nonempty(attempt)}"
    e = f"e AS (SELECT toString({actor}) AS actor,toString({attempt}) AS attempt,event,timestamp AS t,if(timestamp<{timestamp(b)},'prior','current') AS period,{cat} AS category FROM events WHERE {where(p, a, c, steps)} AND {keyed})"
    # Identity chains start at the attempt's first step-1 event. A window chain tries every
    # step-1 event as a start (arrayJoin) and keeps later steps within N hours of it.
    start = "arrayJoin(a1)" if hours else "arrayElement(a1,1)"
    within = f" AND dateDiff('second',t1,x)<={hours * 3600}" if hours else ""
    parts = [
        e,
        "b AS (SELECT DISTINCT actor,attempt,event,t,period FROM e)",
        "stages AS (SELECT period,actor,attempt,"
        + ",".join(
            f"arraySort(groupArrayIf(t,event={literal(ev)})) AS a{i}"
            for i, ev in enumerate(steps, 1)
        )
        + " FROM b GROUP BY period,actor,attempt)",
        f"s1 AS (SELECT *,{start} AS t1,1 AS depth1 FROM stages WHERE length(a1)>0)",
    ]
    for i in range(2, n + 1):
        parts += [
            f"f{i} AS (SELECT *,arrayFilter(x -> depth{i - 1}={i - 1} AND x>t{i - 1}{within},a{i}) AS later{i} FROM s{i - 1})",
            f"s{i} AS (SELECT *,arrayElement(later{i},1) AS t{i},if(length(later{i})>0,{i},depth{i - 1}) AS depth{i} FROM f{i})",
        ]
    times = ",".join(f"t{i}" for i in range(1, n + 1))
    parts += [
        f"attempts AS (SELECT period,actor,attempt,{times},depth{n} AS depth FROM s{n})",
        f"ranked AS (SELECT *,row_number() OVER(PARTITION BY period,actor ORDER BY depth DESC,{times},attempt) AS choice FROM attempts)",
        "chosen AS (SELECT *,formatDateTime(t1,'%Y-%m-%d','UTC') AS day FROM ranked WHERE choice=1)",
    ]
    return parts


def funnel(p, w):
    n = len(p["steps"])
    parts = funnel_ctes(p, w, category=False)
    pairs = list(
        dict.fromkeys([(1, i) for i in range(2, n + 1)] + [(i - 1, i) for i in range(2, n + 1)])
    )
    metrics = [f"countIf(depth>={i}) AS n{i}" for i in range(1, n + 1)]
    metrics += [
        f"arraySort(groupArrayIf(dateDiff('microsecond',t{x},t{y})/1000000.0,depth>={y})) AS d{x}_{y}"
        for x, y in pairs
    ]
    bad = " OR ".join(f"(depth>={i} AND t{i}<=t{i - 1})" for i in range(2, n + 1))
    metrics += [
        f"countIf({bad}) AS order_violations",
        "count()-uniqExact(actor) AS duplicate_choices",
    ]
    parts += [
        "daily AS (SELECT period,day," + ",".join(metrics) + " FROM chosen GROUP BY period,day)"
    ]
    cols = ["period", "day"] + [f"n{i}" for i in range(1, n + 1)]
    cols += [
        f"if(n{y}=0,NULL,(arrayElement(d{x}_{y},intDiv(n{y}+1,2))+arrayElement(d{x}_{y},intDiv(n{y}+2,2)))/2.0) AS median_d{x}_{y}"
        for x, y in pairs
    ]
    cols += ["order_violations", "duplicate_choices"]
    return (
        "WITH "
        + ",\n".join(parts)
        + " SELECT "
        + ",".join(cols)
        + " FROM daily ORDER BY period,day LIMIT 63"
    )


def breakdown(p, w):
    if not p["category_property"]:
        raise ValueError("no defensible category")
    n = len(p["steps"])
    parts = funnel_ctes(p, w)
    parts += [
        f"first_categories AS (SELECT period,actor,t,category,min(t) OVER(PARTITION BY period,actor) AS first_t FROM e WHERE event={literal(p['steps'][0])})",
        "cats AS (SELECT period,actor,if(uniqExactIf(category,t=first_t)=1,anyIf(category,t=first_t),'Ambiguous') AS category_bucket FROM first_categories GROUP BY period,actor)",
        "outcomes AS (SELECT c.period AS period,c.actor AS actor,c.depth AS depth,k.category_bucket AS category FROM chosen c JOIN cats k ON c.period=k.period AND c.actor=k.actor)",
    ]
    return (
        "WITH "
        + ",\n".join(parts)
        + f" SELECT category,count() AS total,countIf(depth={n}) AS converted FROM outcomes WHERE period='current' GROUP BY category ORDER BY category LIMIT 12"
    )


def traffic(p, w):
    if not p["pageview_event"]:
        raise ValueError("no-pageview mapping")
    a, b, c = w
    actor = identity(p["traffic_actor_key"])
    session = identity(p["traffic_chain_key"])
    path = bucket(prop(p["path_property"]), p["paths"])
    ref = prop(p["source_property"])
    hosts = sorted({re.sub(r"^www\.", "", h) for h in p.get("_website_hosts", [])})

    def domain(expr):
        return f"ifNull(replaceRegexpOne(lower(toString({expr})),{regex(WWW)},''),'')"

    internal = "dom=own" + (" OR dom IN (" + ",".join(map(literal, hosts)) + ")" if hosts else "")
    # The builder, not the agent, classifies every entry. See CHANNELS for the order.
    channel = (
        f"multiIf(match(med,{regex(PAID_MEDIUM)}),'Paid',"
        f"match(med,{regex(EMAIL_MEDIUM)}),'Email',"
        f"match(med,{regex(SOCIAL_MEDIUM)}),'Social',"
        "no_ref,'Unknown',dom IN ('','$direct'),'Direct',"
        f"{internal},'Internal',"
        f"match(dom,{regex(AI_DOMAINS)}),'AI assistants',"
        f"match(dom,{regex(SEARCH_DOMAINS)}),'Search',"
        f"match(dom,{regex(SOCIAL_DOMAINS)}),'Social','Referral')"
    )
    first = "t=first_t"
    pair = f"uniqExactIf(tuple(channel,dom),{first})=1"
    # Traffic requires this key's semantics to be session, not attempt. The skill enforces it.
    parts = [
        f"v0 AS (SELECT if(timestamp<{timestamp(b)},'prior','current') AS period,toString({actor}) AS actor,toString({session}) AS session,timestamp AS t,{path} AS path,{domain(ref)} AS dom,isNull({ref}) AS no_ref,{domain(prop('$host'))} AS own,ifNull(lower(toString({prop('utm_medium')})),'') AS med FROM events WHERE {where(p, a, c, [p['pageview_event']])} AND {nonempty(actor)} AND {nonempty(session)})",
        f"v AS (SELECT *,{channel} AS channel FROM v0)",
        "ordered AS (SELECT *,min(t) OVER(PARTITION BY period,actor,session) AS first_t FROM v)",
        f"entries AS (SELECT period,actor,session,if(uniqExactIf(path,{first})=1,anyIf(path,{first}),'Ambiguous') AS entry_path,if({pair},anyIf(channel,{first}),'Ambiguous') AS entry_channel,if({pair},anyIf(dom,{first}),'') AS entry_dom,count() AS pageviews FROM ordered GROUP BY period,actor,session)",
        # Referral domains are named by entry sessions across both periods; the rest are Other.
        "doms AS (SELECT dom,row_number() OVER(ORDER BY n DESC,dom) AS rank FROM (SELECT entry_dom AS dom,count() AS n FROM entries WHERE entry_channel='Referral' AND match(entry_dom,'^[a-z][a-z0-9.-]{0,62}$') AND NOT match(entry_dom,'[0-9]{5}') GROUP BY entry_dom))",
    ]
    return (
        "WITH "
        + ",".join(parts)
        + " SELECT e.period AS period,e.entry_path AS path,e.entry_channel AS channel,if(e.entry_channel='Referral',if(ifNull(d.rank,0) BETWEEN 1 AND 8,e.entry_dom,'Other'),e.entry_channel) AS source,count() AS sessions,sum(e.pageviews) AS pageviews FROM entries e LEFT JOIN doms d ON e.entry_dom=d.dom GROUP BY period,path,channel,source ORDER BY period,path,channel,source LIMIT 400"
    )


def request(c, step, p, w):
    """One analytics.posthog query.hogql call: pass its fields to call_service unchanged."""
    builders = {
        "inventory": inventory,
        "coverage": coverage,
        "trends": trends,
        "funnel": funnel,
        "traffic": traffic,
        "breakdown": breakdown,
        "dimensions": dimensions,
    }
    if step not in builders:
        raise ValueError("unknown query step")
    if step != "inventory":
        validate_plan(p)
    hosts = c.get("website_hosts", [])
    validate_hosts(hosts)
    if hosts and p.get("pageview_event", "$pageview") not in ("", "$pageview"):
        raise ValueError("website_hosts currently scopes the standard $pageview event only")
    # Structured exclusion inputs are compiled by the builder, never left to the agent.
    if any(rule not in p.get("exclusions", []) for rule in input_exclusions(c)):
        raise ValueError("plan exclusions must include input_exclusions(settings)")
    scoped = {**p, "_website_hosts": hosts}
    sql = builders[step](scoped, w)
    if step == "coverage":
        sql = sql[0]
    # Caller supplies validated data, never SQL; query text only comes from fixed builders.
    # Tin's gateway accepts one SELECT of at most 8000 bytes ending in LIMIT n (n <= 1000),
    # without OFFSET; check the same shape here so a refusal never costs a call.
    if not sql.startswith(("SELECT ", "WITH ")) or ";" in sql or "--" in sql or "/*" in sql:
        raise ValueError("invalid builder output")
    limit = re.search(r" LIMIT ([0-9]+)\Z", sql)
    if limit is None or not 1 <= int(limit.group(1)) <= 1000 or re.search(r"\bOFFSET\b", sql):
        raise ValueError("invalid builder output")
    if len(sql.encode()) > 8000:
        raise ValueError("query exceeds the 8000-byte HogQL bound")
    return dict(
        service="analytics",
        step=step,
        operation="query.hogql",
        arguments={"query": sql, "name": "analytics brief " + step},
    )


def properties_request(events):
    """The one analytics.posthog property_definitions.list call, for 1-20 selected events."""
    if (
        type(events) is not list
        or not 1 <= len(events) <= 20
        or len(set(events)) != len(events)
        or any(type(e) is not str or not 1 <= len(e) <= 200 for e in events)
    ):
        raise ValueError("select 1-20 distinct event names")
    return dict(
        service="analytics",
        step="properties",
        operation="property_definitions.list",
        arguments={"event_names": sorted(events), "limit": 100},
    )


def property_types(page):
    """Validate the projected property page; returns {name: type or None} and completeness."""
    if (
        type(page) is not dict
        or type(page.get("records")) is not list
        or type(page.get("has_more")) is not bool
        or type(page.get("truncated")) is not bool
    ):
        raise ValueError("invalid property definitions")
    types = {}
    for r in page["records"]:
        if type(r) is not dict or type(r.get("name")) is not str or r["name"] in types:
            raise ValueError("invalid property definition")
        if r.get("property_type") is not None and type(r["property_type"]) is not str:
            raise ValueError("invalid property type")
        types[r["name"]] = r.get("property_type")
    return {"types": types, "complete": not page["has_more"]}


def check_property_types(found, p):
    """Keys whose declared type contradicts their use. Unlisted is unverified, not absent."""
    wanted = {}
    for key in ("actor_key", "chain_key", "traffic_actor_key", "traffic_chain_key"):
        if p[key].startswith("event:"):
            wanted[p[key][6:]] = "String"
    for key in ("category_property", "path_property", "source_property"):
        if p[key]:
            wanted[p[key]] = "String"
    conflicts, unverified = [], []
    for name, expected in sorted(wanted.items()):
        actual = found["types"].get(name)
        if name not in found["types"] or actual is None:
            unverified.append(name)
        elif actual != expected:
            conflicts.append({"property": name, "type": actual, "expected": expected})
    return {"conflicts": conflicts, "unverified": unverified, "complete": found["complete"]}


def table(data, columns, cap):
    # Pass the query.hogql result unchanged: {columns, types, rows, has_more, truncated}.
    if type(data) is not dict or len(json.dumps(data, ensure_ascii=False).encode()) > 64000:
        raise ValueError("invalid provider data")
    for k in ("has_more", "truncated"):
        if type(data.get(k)) is not bool:
            raise ValueError("invalid provider data")
        if data[k]:
            raise ValueError("incomplete evidence: " + k)
    if data.get("columns") != columns or type(data.get("rows")) is not list:
        raise ValueError("wrong result schema")
    rows = data["rows"]
    if len(rows) >= cap or any(type(row) is not list or len(row) != len(columns) for row in rows):
        raise ValueError("truncated/malformed result")
    return [dict(zip(columns, row)) for row in rows]


def count(x):
    if type(x) is not int or x < 0:
        raise ValueError("invalid count")
    return x


def ratio(n, d):
    count(n)
    count(d)
    return None if d == 0 else n / d


def change(current, prior):
    count(current)
    count(prior)
    return {
        "current": current,
        "prior": prior,
        "delta": current - prior,
        "relative_pct": None if prior == 0 else 100 * (current - prior) / prior,
    }


def validate_funnel(rows, p, w):
    n = len(p["steps"])
    seen = set()
    totals = {"prior": [0] * n, "current": [0] * n}
    a, b, c = w
    for r in rows:
        day = dt.date.fromisoformat(r["day"])
        period = r["period"]
        lo, hi = (a, b) if period == "prior" else (b, c)
        if period not in totals or not lo.date() <= day < hi.date() or (period, day) in seen:
            raise ValueError("invalid cohort day")
        seen.add((period, day))
        nums = [count(r["n" + str(i)]) for i in range(1, n + 1)]
        if (
            any(x < y for x, y in zip(nums, nums[1:]))
            or count(r["order_violations"])
            or count(r["duplicate_choices"])
        ):
            raise ValueError("invalid chain counts")
        for x, y in dict.fromkeys(
            [(1, i) for i in range(2, n + 1)] + [(i - 1, i) for i in range(2, n + 1)]
        ):
            v = r[f"median_d{x}_{y}"]
            if nums[y - 1] == 0:
                if v is not None:
                    raise ValueError("median for empty cohort")
            elif (
                type(v) not in (int, float)
                or not math.isfinite(v)
                or not 0 < v < (hi - lo).total_seconds()
            ):
                raise ValueError("invalid median")
        totals[period] = [x + y for x, y in zip(totals[period], nums)]
    return totals


def validate_coverage(rows, events, w, property_keys):
    seen = set()
    result = {}
    for r in rows:
        k = (r["period"], r["event"])
        if k in seen or k[0] not in ("prior", "current") or k[1] not in events:
            raise ValueError("invalid coverage key")
        seen.add(k)
        raw, ar, er, actors, chains = [
            count(r[x]) for x in ("raw", "actor_rows", "eligible_rows", "actors", "chains")
        ]
        if (
            not count(r["eligible_actors"]) <= actors <= ar <= raw
            or not r["eligible_actors"] <= chains <= er <= ar
        ):
            raise ValueError("invalid coverage counts")
        for i, _ in enumerate(property_keys):
            if count(r["missing_p" + str(i)]) > raw:
                raise ValueError("invalid property coverage")
        if "funnel_actors" in r and not (
            count(r["funnel_eligible_actors"]) <= count(r["funnel_actors"]) <= raw
        ):
            raise ValueError("invalid coverage counts")
        if raw == 0:
            raise ValueError("empty grouped coverage row")
        lo, hi = (w[0], w[1]) if k[0] == "prior" else (w[1], w[2])
        if not lo <= observed_time(r["first_seen"]) <= observed_time(r["last_seen"]) < hi:
            raise ValueError("coverage date mismatch")
        result[k] = r
    return result


def validate_trends(rows, p, w, cov):
    a, b, c = w
    hist = c - dt.timedelta(days=90)
    seen = set()
    sums = {}
    series = {}
    events = sorted(set(p["key_events"] + p["error_events"]))
    for r in rows:
        day = dt.date.fromisoformat(r["day"])
        ev = r["event"]
        res = r["resolution"]
        k = (r["day"], res, ev)
        if k in seen or ev not in events:
            raise ValueError("invalid trend key")
        if res == "day" and not a.date() <= day < c.date():
            raise ValueError("invalid daily trend date")
        if res == "week" and (
            day.weekday() != 0 or not hist.date() - dt.timedelta(days=6) <= day < a.date()
        ):
            raise ValueError("invalid weekly trend date")
        if res not in ("day", "week"):
            raise ValueError("invalid trend resolution")
        seen.add(k)
        raw, actors, chains = [count(r[x]) for x in ("raw", "actors", "chains")]
        if max(actors, chains) > raw:
            raise ValueError("invalid trend aggregates")
        if res == "day":
            period = "prior" if day < b.date() else "current"
            sums[(period, ev)] = sums.get((period, ev), 0) + raw
        series[k] = r
    for period in ("prior", "current"):
        for ev in events:
            if sums.get((period, ev), 0) != cov.get((period, ev), {}).get("raw", 0):
                raise ValueError("trend/coverage mismatch")
    for i in range((c - a).days):
        day = (a + dt.timedelta(days=i)).date().isoformat()
        for ev in events:
            series.setdefault(
                (day, "day", ev),
                dict(day=day, resolution="day", event=ev, raw=0, actors=0, chains=0),
            )
    comparisons = {
        ev: change(sums.get(("current", ev), 0), sums.get(("prior", ev), 0)) for ev in events
    }
    return sorted(
        series.values(), key=lambda r: (r["day"], r["resolution"], r["event"])
    ), comparisons


def validate_breakdown(rows, p, current_funnel):
    seen = set()
    n = success = 0
    for r in rows:
        cat = r["category"]
        if cat in seen or cat not in set(p["categories"]) | RESERVED:
            raise ValueError("invalid category")
        seen.add(cat)
        count(r["total"])
        count(r["converted"])
        if r["converted"] > r["total"]:
            raise ValueError("impossible conversion")
        n += r["total"]
        success += r["converted"]
    if (n, success) != (current_funnel[0], current_funnel[-1]):
        raise ValueError("breakdown/funnel mismatch")
    return n, success


def validate_traffic(rows, p, cov):
    seen, named = set(), set()
    totals = {x: [0, 0] for x in ("prior", "current")}
    for r in rows:
        k = (r["period"], r["path"], r["channel"], r["source"])
        if (
            k in seen
            or k[0] not in totals
            or k[1] not in set(p["paths"]) | RESERVED
            or k[2] not in set(CHANNELS) | {"Unknown", "Ambiguous"}
        ):
            raise ValueError("invalid traffic bucket")
        # Only Referral rows name a domain; every other row repeats its channel.
        if k[2] != "Referral" and k[3] != k[2]:
            raise ValueError("invalid traffic source")
        if k[2] == "Referral" and k[3] != "Other":
            if not re.fullmatch(r"[a-z][a-z0-9.-]{0,62}", k[3]):
                raise ValueError("invalid referring domain")
            safe_dimension(k[3])
            named.add(k[3])
        seen.add(k)
        n = count(r["sessions"])
        v = count(r["pageviews"])
        if n > v:
            raise ValueError("sessions exceed pageviews")
        totals[k[0]][0] += n
        totals[k[0]][1] += v
    if len(named) > 8:
        raise ValueError("too many named referring domains")
    for period, (n, v) in totals.items():
        r = cov.get((period, p["pageview_event"]), {})
        if n != r.get("chains", 0) or v != r.get("eligible_rows", 0):
            raise ValueError("traffic/coverage mismatch")
    return totals


def traffic_summary(rows):
    """Entry sessions and their pageviews by channel, named referrer and landing path."""
    out = {x: {"channels": {}, "referrers": {}, "paths": {}} for x in ("prior", "current")}
    for r in rows:
        keys = [("channels", r["channel"]), ("paths", r["path"])]
        if r["channel"] == "Referral":
            keys.append(("referrers", r["source"]))
        for kind, key in keys:
            sessions, pageviews = out[r["period"]][kind].get(key, (0, 0))
            out[r["period"]][kind][key] = [sessions + r["sessions"], pageviews + r["pageviews"]]
    return out


def fisher(a, b, c, d):
    return fisher_exact_two_sided(a, b, c, d)


def screen(rows, plan):
    total = sum(count(r["total"]) for r in rows)
    yes = sum(count(r["converted"]) for r in rows)
    by_name = {r["category"]: r for r in rows}
    family = plan["categories"]
    if not family:
        return {
            "status": "insufficient evidence",
            "test": "two-sided Fisher exact; Holm correction",
            "family_size": 0,
            "results": [],
        }
    tables = []
    for name in family:
        r = by_name.get(name, {"total": 0, "converted": 0})
        a = r["converted"]
        b = r["total"] - a
        c = yes - a
        d = total - r["total"] - c
        tables.append((a, b, c, d))
    checked = screen_comparisons(tables)
    return {
        "status": "screened",
        "test": "two-sided Fisher exact; Holm correction",
        "family_size": len(family),
        "alpha": 0.05,
        "results": [
            dict(category=name, n=a + b, converted=a, rate=ratio(a, a + b), **v)
            for name, (a, b, c, d), v in zip(family, tables, checked, strict=True)
        ],
    }


def reference_funnel(rows, steps, window_seconds=None):
    # Synthetic oracle only: rows=(period,actor,attempt,event,numeric_time).
    # Exclusions and UTC half-open boundaries must already have been applied.
    # With window_seconds, attempts are ignored: every step-1 event may start a chain and
    # later steps count only within that many seconds of it.
    groups = {}
    for period, actor, attempt, event, t in rows:
        if window_seconds is not None:
            attempt = "window"
        if actor is None or attempt is None or actor == "" or attempt == "":
            continue
        groups.setdefault((period, actor, attempt), set()).add((event, t))
    chosen = {}
    for (period, actor, attempt), events in groups.items():
        starts = sorted(t for e, t in events if e == steps[0])
        for start in starts if window_seconds is not None else starts[:1]:
            times = [start]
            for ev in steps[1:]:
                valid = sorted(
                    t
                    for e, t in events
                    if e == ev
                    and t > times[-1]
                    and (window_seconds is None or t - start <= window_seconds)
                )
                if not valid:
                    break
                times.append(valid[0])
            rank = (-len(times), tuple(times), attempt)
            if (period, actor) not in chosen or rank < chosen[(period, actor)][0]:
                chosen[(period, actor)] = (rank, times)
    return {
        period: {
            "counts": [
                sum(len(t) >= i for (per, _), (_, t) in chosen.items() if per == period)
                for i in range(1, len(steps) + 1)
            ],
            "medians": [
                statistics.median(
                    [
                        t[i] - t[0]
                        for (per, _), (_, t) in chosen.items()
                        if per == period and len(t) > i
                    ]
                )
                if any(per == period and len(t) > i for (per, _), (_, t) in chosen.items())
                else None
                for i in range(1, len(steps))
            ],
        }
        for period in ("prior", "current")
    }


def observed_time(value):
    if type(value) is not str:
        raise ValueError("invalid observed timestamp")
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("provider timestamp timezone unverified")
    return t.astimezone(dt.timezone.utc)


def inventory_scope(rows):
    if not rows:
        return {"returned_event_types": 0, "total_event_types": 0, "complete": True}
    totals = {count(r["total_event_types"]) for r in rows}
    if len(totals) != 1:
        raise ValueError("inconsistent inventory total")
    total = totals.pop()
    if len(rows) != min(total, 200):
        raise ValueError("incomplete inventory response")
    return {
        "returned_event_types": len(rows),
        "total_event_types": total,
        "complete": len(rows) == total,
    }


def validate_inventory(rows, w):
    inventory_scope(rows)
    a, b, c = w
    lo = c - dt.timedelta(days=90)
    seen = {}
    for r in rows:
        ev = r["event"]
        if type(ev) is not str or not 1 <= len(ev) <= 256 or ev in seen:
            raise ValueError("invalid inventory event")
        observed, prior, current = [count(r[k]) for k in ("observed", "prior", "current")]
        if observed == 0 or prior + current > observed:
            raise ValueError("invalid inventory counts")
        first, last = observed_time(r["first_seen"]), observed_time(r["last_seen"])
        if not lo <= first <= last < c:
            raise ValueError("invalid observed range")
        seen[ev] = r
    return seen


def reconcile_coverage(cov, inventory_rows, p, w):
    inventory_map = validate_inventory(inventory_rows, w)
    complete = inventory_scope(inventory_rows)["complete"]
    a, b, c = w
    for period, lo, hi in (("prior", a, b), ("current", b, c)):
        for ev in event_list(p):
            r = cov.get((period, ev), {})
            # Omission from bounded discovery is not evidence of zero events. Coverage,
            # trends and funnel queries still scan the full selected event/window.
            if (ev in inventory_map or complete) and r.get("raw", 0) != inventory_map.get(
                ev, {}
            ).get(period, 0):
                raise ValueError("inventory/coverage mismatch")
            if r:
                first, last = observed_time(r["first_seen"]), observed_time(r["last_seen"])
                if not lo <= first <= last < hi:
                    raise ValueError("coverage date mismatch")
    return cov


def funnel_coverage(cov, period, event, key):
    # A split pageview step reports its activation identity in funnel_* columns.
    r = cov.get((period, event), {})
    return r.get("funnel_" + key, r.get(key, 0))


def reconcile_funnel(rows, p, w, cov):
    totals = validate_funnel(rows, p, w)
    for period, nums in totals.items():
        for i, ev in enumerate(p["steps"]):
            eligible = funnel_coverage(cov, period, ev, "eligible_actors")
            if nums[i] > eligible or (i == 0 and nums[i] != eligible):
                raise ValueError("funnel/coverage mismatch")
        # Actors did the final step and others started the funnel, yet no chain joined them:
        # the identity keys do not link these events. Report it; never show it as 0%.
        finished = funnel_coverage(cov, period, p["steps"][-1], "actors")
        if nums[0] and finished and not nums[-1]:
            raise ValueError(
                f"unjoinable identity: in the {period} window {nums[0]} actors started the "
                f"funnel and {finished} did its final step, but none joined a chain"
            )
    # Daily zero-fill is valid only after successful table/schema/coverage reconciliation.
    present = {(r["period"], r["day"]): r for r in rows}
    a, b, c = w
    for period, lo, hi in (("prior", a, b), ("current", b, c)):
        for i in range((hi - lo).days):
            day = (lo + dt.timedelta(days=i)).date().isoformat()
            if (period, day) not in present:
                empty = dict(period=period, day=day, order_violations=0, duplicate_choices=0)
                empty.update({"n" + str(j): 0 for j in range(1, len(p["steps"]) + 1)})
                empty.update({f"median_d{x}_{y}": None for x, y in median_pairs(len(p["steps"]))})
                present[(period, day)] = empty
    return totals, sorted(present.values(), key=lambda r: (r["day"], r["period"]))


def plan_state(previous, binding, plan, schema_signature):
    validate_plan(plan)
    encoded = json.dumps(schema_signature, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode()) > 8000:
        raise ValueError("schema signature too large")
    proposed = {
        "binding": binding,
        "version": VERSION,
        "plan": plan,
        "schema_signature": schema_signature,
    }
    if previous is None:
        return {"state": "provisional", "pin": proposed, "changes": []}
    if type(previous) is not dict or set(previous) != set(proposed):
        raise ValueError("invalid previous pin")
    validate_plan(previous["plan"])
    # An explicit saved-input edit authorizes a new mapping, not a new software release.
    if previous["binding"] != binding:
        return {"state": "reconfigured", "pin": proposed, "changes": ["saved inputs"]}
    changes = [k for k in proposed if proposed[k] != previous[k]]
    detail = []
    for k in changes:
        detail += (
            ["plan." + name for name in plan if plan[name] != previous["plan"].get(name)]
            if k == "plan"
            else [k]
        )
    return {
        "state": "schema changed" if changes else "pinned",
        "pin": previous,
        "proposed": proposed if changes else None,
        "changes": detail,
    }


def funnel_display(rows, plan):
    answer = []
    for r in rows:
        first = r["n1"]
        for i, (ev, label) in enumerate(zip(plan["steps"], plan["labels"]), 1):
            n = r["n" + str(i)]
            previous = r.get("n" + str(i - 1))
            answer.append(
                {
                    "period": r["period"],
                    "selected_start_day": r["day"],
                    "step": i,
                    "event": ev,
                    "label": label,
                    "chain_actors": n,
                    "of_first_pct": None if first == 0 else 100 * ratio(n, first),
                    "of_previous_pct": None
                    if previous is None or previous == 0
                    else 100 * ratio(n, previous),
                    "median_from_first_seconds": (0 if n else None)
                    if i == 1
                    else r[f"median_d1_{i}"],
                    "median_from_previous_seconds": None if i == 1 else r[f"median_d{i - 1}_{i}"],
                }
            )
    return answer


def coverage_display(cov, property_keys):
    answer = []
    for (period, event), r in sorted(cov.items()):
        raw = r["raw"]
        answer.append(
            {
                "period": period,
                "event": event,
                "raw": raw,
                "missing_actor_rows": raw - r["actor_rows"],
                "missing_actor_or_chain_rows": raw - r["eligible_rows"],
                "actor_coverage": ratio(r["actor_rows"], raw),
                "chain_coverage": ratio(r["eligible_rows"], raw),
                "missing_properties": {
                    name: r["missing_p" + str(i)] for i, name in enumerate(property_keys)
                },
            }
        )
    return answer


def exclusion_field(name):
    if name == "distinct_id":
        return "distinct_id"
    return prop(name[7:], "person.properties") if name.startswith("person:") else prop(name)


def median_pairs(n):
    return list(
        dict.fromkeys([(1, i) for i in range(2, n + 1)] + [(i - 1, i) for i in range(2, n + 1)])
    )


def query_columns(step, p):
    if step == "inventory":
        return [
            "event",
            "observed",
            "prior",
            "current",
            "first_seen",
            "last_seen",
            "total_event_types",
        ], 201
    if step == "coverage":
        _, keys = coverage(p, settings({"as_of_utc": "2026-01-01"})[1])
        return [
            "event",
            "period",
            "raw",
            "actor_rows",
            "eligible_rows",
            "actors",
            "eligible_actors",
            "chains",
            "first_seen",
            "last_seen",
        ] + ["missing_p" + str(i) for i in range(len(keys))] + (
            ["funnel_actors", "funnel_eligible_actors"] if split_pageview(p) else []
        ), 41
    if step == "trends":
        return ["day", "resolution", "event", "raw", "actors", "chains"], 601
    if step == "funnel":
        n = len(p["steps"])
        return ["period", "day"] + [f"n{i}" for i in range(1, n + 1)] + [
            f"median_d{x}_{y}" for x, y in median_pairs(n)
        ] + ["order_violations", "duplicate_choices"], 63
    if step == "traffic":
        return ["period", "path", "channel", "source", "sessions", "pageviews"], 400
    if step == "breakdown":
        return ["category", "total", "converted"], 12
    if step == "dimensions":
        return ["kind", "value", "volume"], 25
    raise ValueError("unknown query schema")


def safe_dimension(value):
    if (
        type(value) is not str
        or not re.fullmatch(r"[A-Za-z/][A-Za-z0-9 _./-]{0,63}", value)
        or value in RESERVED
        or re.search(r"[0-9]{5}|[a-f0-9]{8}-[a-f0-9]{4}", value, re.I)
    ):
        raise ValueError("unsafe or identifying dimension value")
    return value


def validate_dimensions(rows, p):
    result = {k: [] for k in ("categories", "paths")}
    seen = set()
    for r in rows:
        kind, value = r["kind"], r["value"]
        if kind not in result or type(value) is not str or (kind, value) in seen:
            raise ValueError("invalid safe dimension value")
        safe_dimension(value)
        count(r["volume"])
        seen.add((kind, value))
        result[kind].append(value)
        if len(result[kind]) > 8:
            raise ValueError("too many dimension values")
    return result


def error_signals(series, plan, funnel_totals):
    # Descriptive heuristics, not bug diagnoses or a matched-cohort failure rate.
    rows = []
    for event in plan["error_events"]:
        counts = [r["raw"] for r in series if r["event"] == event and r["resolution"] == "day"]
        baseline = statistics.median(counts) if counts else 0
        if counts and max(counts) >= 20 and max(counts) >= 5 * max(1, baseline):
            rows.append(
                {
                    "event": event,
                    "signal": "daily concentration",
                    "peak": max(counts),
                    "median": baseline,
                    "rule": "peak >= 20 and >= 5 x max(1, median)",
                }
            )
    if funnel_totals:
        for period, values in funnel_totals.items():
            rows.append(
                {
                    "period": period,
                    "signal": "no observed completion within window",
                    "actors": values[0] - values[-1],
                }
            )
    return rows


def dimensions(p, w):
    # Label discovery uses volume, never conversion, and exports no raw identifiers. One SELECT
    # (Tin's gateway refuses UNION): each event row yields a [kind, value] pair per supported
    # dimension, and the eight highest-volume safe values per kind are kept. Traffic sources
    # are not discovered here: traffic() classifies channels and names referrers itself.
    pairs, selected = [], set()
    a, b, c = w
    for kind, key, events in [
        ("categories", p["category_property"], p["steps"][:1]),
        ("paths", p["path_property"], [p["pageview_event"]]),
    ]:
        if not key or not all(events):
            continue
        expr = f"toString({prop(key)})"
        safe = f"match({expr},'^[A-Za-z/][A-Za-z0-9 _./-]{{0,63}}$') AND NOT match({expr},'[0-9]{{5}}|[a-fA-F0-9]{{8}}-[a-fA-F0-9]{{4}}') AND {expr} NOT IN ('Unknown','Other','Ambiguous')"
        chosen = "event IN (" + ",".join(map(literal, events)) + ")"
        pairs.append(f"if({chosen} AND coalesce({safe},false),['{kind}',{expr}],['',''])")
        selected.update(events)
    if not pairs:
        raise ValueError("no supported dimension fields")
    rows = f"SELECT arrayJoin([{','.join(pairs)}]) AS pair FROM events WHERE {where(p, a, c, sorted(selected))}"
    counted = f"SELECT arrayElement(pair,1) AS kind,arrayElement(pair,2) AS value,count() AS volume FROM ({rows}) WHERE arrayElement(pair,1)!='' GROUP BY kind,value"
    ranked = f"SELECT kind,value,volume,row_number() OVER(PARTITION BY kind ORDER BY volume DESC,value) AS rank FROM ({counted})"
    return f"SELECT kind,value,volume FROM ({ranked}) WHERE rank<=8 ORDER BY kind,volume DESC,value LIMIT 25"


def public_pin(pin):
    """Keep exclusions comparable without publishing their values, domains included."""
    copy = json.loads(json.dumps(pin))
    for rule in copy["plan"]["exclusions"]:
        rule["value"] = {"sha256": stable_hash(rule["value"])}
    return copy


def restore_pin(pin, binding, rules):
    if not isinstance(pin, dict) or pin.get("binding") != binding:
        raise ValueError("previous settings binding mismatch")
    exclusions(rules)
    copy = json.loads(json.dumps(pin))
    expected = [
        {"property": r["property"], "op": r["op"], "value": {"sha256": stable_hash(r["value"])}}
        for r in rules
    ]
    if copy.get("plan", {}).get("exclusions") != expected:
        raise ValueError("previous exclusions mismatch")
    copy["plan"]["exclusions"] = json.loads(json.dumps(rules))
    validate_plan(copy["plan"])
    return copy


def safe_sql(sql, rules):
    # Remove complete exclusion clauses, rather than accidentally rewriting event literals.
    exclusions(rules)
    for rule in rules:
        sql = sql.replace(exclusion_clause(rule), "[exclusion " + stable_hash(rule) + "]")
    return sql


def request_record(request, rules, outcome, message="", data=None):
    """One executed call for the evidence requests list. outcome is ok, refused (Tin's tool
    error) or invalid (table() or a validator rejected the response)."""
    if outcome not in ("ok", "refused", "invalid"):
        raise ValueError("outcome must be ok, refused or invalid")
    record = {
        "step": request["step"],
        "operation": request["operation"],
        "outcome": outcome,
        "message": " ".join(str(message).split())[:500],
    }
    if request["operation"] == "query.hogql":
        record["query"] = safe_sql(request["arguments"]["query"], rules)
    if data is not None:
        record.update({k: data.get(k) for k in ("columns", "rows", "has_more", "truncated")})
    return record


def render_evidence(status, reason, fields):
    """The closing evidence block. Tin checks it (analytics-brief.v1) before publishing."""
    reason = " ".join(str(reason).split())
    if status not in STATUSES or (status == "complete") == bool(reason) or len(reason) > 180:
        raise ValueError("status must be a REPORT.md status; only non-complete ones take a reason")
    wanted = {
        "binding",
        "generated_at",
        "windows",
        "state",
        "inventory_scope",
        "requests",
        "derived",
        "limitations",
    }
    if set(fields) != wanted:
        raise ValueError("evidence fields differ from REPORT.md")
    body = {"status": status, "status_reason": reason, "provider": "analytics.posthog", **fields}
    fence = "`" * 3
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"{EVIDENCE_MARKER}\n{fence}json\n{text}\n{fence}\n"
```
