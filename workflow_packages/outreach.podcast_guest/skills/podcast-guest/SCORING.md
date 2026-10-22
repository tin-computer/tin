# Bookkeeping: balance, exclusions, memory and the pitch block

Extract the single Python block below into a scratch module and use it unchanged. You judge
each show (its arena, how well it fits, its rank within the arena, the route in and the
notes); this code checks the records, merges duplicates, applies the founder's exclusions and
memory, balances the picks across arenas, reports gaps and checks the pitch block for stage
two. It never ranks shows across arenas: a 5,000-listener Turkish show and a 100,000-listener
US show are not on one scale.

## Arena records

One per audience you chose in SKILL.md step 3:

- `id`: short lowercase id (`indie_devs`, `ai_engineers`, `tr_tech`).
- `label`: how the report names it.
- `kind`: `buyers` (people who would use the product), `craft` (the founder's own trade),
  `home` (the founder's home country or language), `topic` (the product's subject) or `other`.
- `language`: the language its shows are in, as a code (`en`, `tr`).
- `why`: one line on why the founder counts in this audience, relative to the people in it.
- `tried`: every method from `METHODS` you tried for it, including ones that found nothing
  or could not run. Optional; the gap check counts these with the methods that found shows.

## Show records

One per candidate you verified or rejected:

- `name`: the show's own name.
- `url`: the show's own page.
- `arena`: the `id` of the arena it serves.
- `language`: the language of the show.
- `last_episode`: `YYYY-MM-DD` of the newest episode you saw, or `null` if you could not tell.
- `episode_dates`: the newest episode dates, newest first, from `read_feed()` or the show's
  own episode list (at least two when the show has them). The first must equal
  `last_episode`. `plan()` uses them to tell a show that paused from one that publishes
  monthly. Optional, but without them a pause goes unnoticed.
- `found_by`: the methods that surfaced it, from `METHODS`.
- `similar_guest`: `{"name", "episode", "url", "date"}` for a past guest who resembles the
  founder in this arena, or `null`. `url` is the episode's page, or the show's page when its
  episodes have none.
- `hook_episode`: `{"title", "url", "date", "detail"}` for the episode the pitch opens on, when
  it is not the `similar_guest` episode. `detail` is what that episode's page, description or
  transcript actually says, in a line; the pitch may only refer to what `detail` holds.
  Optional; without it the pitch opens on the `similar_guest` episode.
- `gap`: what the show's catalogue lacks that this pitch offers (a sequel not yet run, a kind
  of guest it has not had, a new season), from the episodes you read. Optional; a pitch may
  open on it instead of an episode.
- `premise`: what the show is, in a sentence or two: the argument or job it is about.
- `host`: who hosts it (name, role, background) and why that matters for this pitch.
- `takes_guests`: `false` for a show that only has its hosts talking. Optional; a show with
  `false` is listed as excluded.
- `route`, the best way in, in this order of preference: `guest_form` or `published_email`
  (the show's own pages ask for guests through a form, or at an address); `contact_email`
  (an address of the host or producer that the show's feed, its own site or its publisher's
  contact page gives, with no stated guest intake); `direct_message` (a host's social
  profile, only when no address exists); `warm_intro` (someone the founder knows is
  connected) or `ladder` (a step comes first: a talk, a guest post, a launch).
- `tier`: `likely` (it books people like the founder, so a good pitch should land) or
  `reach` (a show with a bigger name or audience than the founder's usual rooms: a chart
  show, a known network, guests a step above the founder; worth a try, likely a no).
  Optional; `likely` when left out.
- `rank`: your rank for it within its arena and tier, 1 first.
- `reason`: why it fits, in one line.
- `pitch_address` and `address_source`: for `published_email` and `contact_email`, the
  address and the page or feed that gives it. Never guessed, never Podscan's `listed_email`
  alone, never a hosting service's or a template's address (the code rejects those).
- `ladder_step` and `deadline`: for `ladder`, the step and its date if it has one.
- `vendor_paid_only`: `{"terms", "source"}` when the show's own pages say a company's people
  come on only as paid sponsors ("vendors, see our sponsorship section", a sponsored
  interview as the only guest offer), with the price or terms and the page that says so.
  `plan()` leaves the show out with that reason. Leave it out when an unpaid way in exists
  (a topic that is not about the product, a practitioner panel); record that way in `notes`
  and pitch it.
- `conflict`: `{"kind", "detail"}` when the publisher sells to the same buyers or competes
  with the product (`competitor`), or when most guests are the publisher's own staff or
  clients (`own_circle`). `plan()` lowers the show's odds by one level.
- `notes`: anything else the founder should weigh: a guest fee or sponsored-episode signs, a
  data caveat. Optional. Notes never exclude a show.

`plan()` returns each pick with `cautions`: why its odds are lower than its tier says (a pause
in publishing, a conflict). A cautioned `likely` show becomes a long shot, and the report's
`Odds` line names each caution. A pick an earlier report drafted carries `drafted_before`.

## Memory between runs

Every report ends with a `tin-podcast-state` block. Read the block of every earlier report in
the output folder, pass each parsed value to `read_state()`, and merge the trusted ones with
`merge_states()`. A block that fails `read_state()` is ignored, and the report says so.

The state records what was drafted, not what was sent: this run sends nothing. A show an
earlier report drafted stays a candidate, after the shows no report has drafted. Only a show
marked `sent` (by the send step, or because the founder lists it in `already_pitched`) is
left out as pitched.

## Feeds and links

`read_feed()` and `check_links()` make public GETs through the run's proxy. Read the feed of
every finalist (its address is on the show's directory page or in Podscan's record) for its
newest episode dates and its owner's address. Before finishing, run `check_links()` on the
whole report.

```python
import re
import unicodedata
from datetime import date

KINDS = ("buyers", "craft", "home", "topic", "other")
METHODS = (
    "topic_sweep",
    "lookalike_company",
    "lookalike_title",
    "buyer_guests",
    "transcript_topic",
    "country_chart",
    "category_chart",
    "local_youtube",
    "named_show",
    "web_search",
)
ROUTES = (
    "published_email",
    "contact_email",
    "guest_form",
    "direct_message",
    "warm_intro",
    "ladder",
)
EMAIL_ROUTES = ("published_email", "contact_email")
TIERS = ("likely", "reach")
CONFLICTS = ("competitor", "own_circle")
# Addresses a hosting service or a template puts in a feed or page: nobody reads them.
PLACEHOLDER_EMAIL = re.compile(
    r"(^(no-?reply|do-?not-?reply|feeds?|podcasts?-?feed)@)"
    r"|@(.+\.)?(anchor\.fm|spreaker\.com|buzzsprout\.com|libsyn\.com|megaphone\.fm|podbean\.com"
    r"|transistor\.fm|simplecast\.com|captivate\.fm|substack\.com|soundcloud\.com)$"
    r"|yourcompany|yourdomain",
    re.I,
)
# A show this many usual gaps past its newest episode has paused; at least this many days.
QUIET_GAPS = 2
QUIET_MIN_DAYS = 14
# About a third of the picks are long shots: enough to try for the bigger shows, not so many
# that the founder spends the week on pitches that mostly go unanswered.
REACH_SHARE = 0.3
HARD_NOS = (
    "no_cold_email",
    "no_paid_ads",
    "no_founder_posting",
    "no_discounting",
    "no_unbacked_claims",
)
# A show with no episode in this window is not booking guests now.
ACTIVE_DAYS = 120
OUTPUT_DIR = "reports/outreach/podcasts/"
FIT_MIN_PICKS = 5
MAX_STATE_SHOWS = 500
EMAIL = re.compile(r"[^@\s<>\"]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}\Z")
ARENA_ID = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
LANGUAGE = re.compile(r"[a-z]{2,3}\Z")


def _day(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error


def _text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be 1-{limit} characters")
    return value.strip()


def _url(value, field):
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        raise ValueError(f"{field} must be an http(s) URL")
    return value


def report_path_of(value):
    """The report's path inside the project, even when given as a sandbox path."""
    if not isinstance(value, str) or not value.endswith(".md"):
        raise ValueError(f"report_path must be the declared output path, {OUTPUT_DIR}<run>.md")
    at = value.find(OUTPUT_DIR)
    if at < 0 or "/" in value[at + len(OUTPUT_DIR) :]:
        raise ValueError(f"report_path must be the declared output path, {OUTPUT_DIR}<run>.md")
    return value[at:]


def slug(name):
    """Normalise a show name for matching: case, accents, punctuation, a leading 'the'."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be non-empty text")
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"^the\s+", "", text)
    if not text:
        raise ValueError("name must contain letters or digits")
    return text


def check_arena(arena):
    if not isinstance(arena, dict):
        raise ValueError("arena must be an object")
    if not isinstance(arena.get("id"), str) or not ARENA_ID.fullmatch(arena["id"]):
        raise ValueError("arena id must be a short lowercase id")
    _text(arena.get("label"), "arena label", 80)
    if arena.get("kind") not in KINDS:
        raise ValueError(f"arena kind must be one of {', '.join(KINDS)}")
    if not isinstance(arena.get("language"), str) or not LANGUAGE.fullmatch(arena["language"]):
        raise ValueError("arena language must be a code such as en or tr")
    _text(arena.get("why"), "arena why", 400)
    tried = arena.get("tried", [])
    if not isinstance(tried, list) or set(tried) - set(METHODS):
        raise ValueError(f"arena tried must list methods from {', '.join(METHODS)}")


def check_show(show, arena_ids):
    if not isinstance(show, dict):
        raise ValueError("show must be an object")
    slug(show.get("name"))
    _url(show.get("url"), "url")
    if show.get("arena") not in arena_ids:
        raise ValueError("arena must be the id of a declared arena")
    if not isinstance(show.get("language"), str) or not LANGUAGE.fullmatch(show["language"]):
        raise ValueError("language must be a code such as en or tr")
    if show.get("last_episode") is not None:
        _day(show["last_episode"], "last_episode")
    found = show.get("found_by")
    if not isinstance(found, list) or not found or set(found) - set(METHODS):
        raise ValueError(f"found_by must list methods from {', '.join(METHODS)}")
    guest = show.get("similar_guest")
    if guest is not None:
        if not isinstance(guest, dict):
            raise ValueError("similar_guest must be an object or null")
        _text(guest.get("name"), "similar_guest name", 120)
        _text(guest.get("episode"), "similar_guest episode", 300)
        _url(guest.get("url"), "similar_guest url")
        _day(guest.get("date"), "similar_guest date")
    hook = show.get("hook_episode")
    if hook is not None:
        if not isinstance(hook, dict):
            raise ValueError("hook_episode must be an object or null")
        _text(hook.get("title"), "hook_episode title", 300)
        _url(hook.get("url"), "hook_episode url")
        _day(hook.get("date"), "hook_episode date")
        _text(hook.get("detail"), "hook_episode detail", 400)
    for field, limit in (("gap", 300), ("premise", 400), ("host", 500)):
        if show.get(field) is not None:
            _text(show[field], field, limit)
    dates = show.get("episode_dates")
    if dates is not None:
        if not isinstance(dates, list) or not 1 <= len(dates) <= 5:
            raise ValueError("episode_dates must list 1-5 dates, newest first")
        days = [_day(item, "episode_dates") for item in dates]
        if days != sorted(days, reverse=True):
            raise ValueError("episode_dates must be newest first")
        if show.get("last_episode") != dates[0]:
            raise ValueError("last_episode must be the newest of episode_dates")
    route = show.get("route")
    if route not in ROUTES:
        raise ValueError(f"route must be one of {', '.join(ROUTES)}")
    if show.get("tier", "likely") not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    rank = show.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("rank must be a whole number from 1")
    _text(show.get("reason"), "reason", 400)
    if route in EMAIL_ROUTES:
        address = show.get("pitch_address")
        if not isinstance(address, str) or not EMAIL.fullmatch(address):
            raise ValueError(f"{route} needs the pitch_address the show or its feed publishes")
        if PLACEHOLDER_EMAIL.search(address):
            raise ValueError(f"{address} is a hosting or template address, not a person's")
        _url(show.get("address_source"), "address_source")
    elif show.get("pitch_address") is not None:
        raise ValueError("only email routes carry a pitch_address")
    paid = show.get("vendor_paid_only")
    if paid is not None:
        if not isinstance(paid, dict):
            raise ValueError("vendor_paid_only must be an object or null")
        _text(paid.get("terms"), "vendor_paid_only terms", 200)
        _url(paid.get("source"), "vendor_paid_only source")
    conflict = show.get("conflict")
    if conflict is not None:
        if not isinstance(conflict, dict) or conflict.get("kind") not in CONFLICTS:
            raise ValueError(f"conflict kind must be one of {', '.join(CONFLICTS)}")
        _text(conflict.get("detail"), "conflict detail", 300)
    if route == "ladder":
        _text(show.get("ladder_step"), "ladder_step", 400)
    if show.get("deadline") is not None:
        _day(show["deadline"], "deadline")
    if show.get("notes") is not None:
        _text(show["notes"], "notes", 600)
    if show.get("takes_guests", True) is not True and show.get("takes_guests") is not False:
        raise ValueError("takes_guests must be true or false")


def read_state(value):
    """Validate one earlier report's state block. Returns None when it cannot be trusted.

    Version 1 blocks (before 1.4.0) called the day a pitch was drafted `first_pitched`,
    although nothing had been sent; they are read as drafts. `sent` is set only when a pitch
    was actually sent.
    """
    try:
        if not isinstance(value, dict) or value.get("version") not in (1, 2):
            return None
        drafted_field = "first_pitched" if value["version"] == 1 else "first_drafted"
        shows = value.get("shows")
        if not isinstance(shows, dict) or len(shows) > MAX_STATE_SHOWS:
            return None
        clean = {}
        for key, item in shows.items():
            if not isinstance(key, str) or key != slug(item.get("name")):
                return None
            if item.get("route") not in ROUTES[:-1]:
                return None
            _day(item.get(drafted_field), drafted_field)
            if item.get("sent") is not None:
                _day(item["sent"], "sent")
            report = item.get("report")
            if not isinstance(report, str) or not report.strip():
                return None
            # Reports from before 1.1.0 could record the sandbox path of the same file.
            try:
                report = report_path_of(report)
            except ValueError:
                pass
            clean[key] = {
                "name": item["name"],
                "route": item["route"],
                "first_drafted": item[drafted_field],
                "sent": item.get("sent"),
                "report": report,
            }
        return {"version": 2, "shows": clean}
    except (AttributeError, TypeError, KeyError, ValueError):
        return None


def merge_states(states):
    """Union trusted earlier states; a show keeps its first draft and its first send."""
    merged = {}
    for state in states:
        if state is None:
            continue
        for key, item in state["shows"].items():
            known = merged.get(key)
            if known is None:
                merged[key] = dict(item)
                continue
            if item["first_drafted"] < known["first_drafted"]:
                merged[key] = dict(item, sent=known["sent"])
            sent = [day for day in (item["sent"], known["sent"]) if day]
            merged[key]["sent"] = min(sent) if sent else None
    return {"version": 2, "shows": merged}


def cautions_of(row, today):
    """What lowers a show's odds: a pause in publishing, or a publisher that competes."""
    found = []
    dates = row.get("episode_dates") or []
    if len(dates) >= 2:
        days = [_day(item, "episode_dates") for item in dates]
        gaps = sorted((a - b).days for a, b in zip(days, days[1:]))
        usual = max(1, gaps[len(gaps) // 2])
        since = (today - days[0]).days
        if since > max(QUIET_GAPS * usual, QUIET_MIN_DAYS):
            found.append(
                f"quiet: no episode since {dates[0]}, though it usually publishes every "
                f"{usual} days"
            )
    conflict = row.get("conflict")
    if conflict:
        lead = "competes with you" if conflict["kind"] == "competitor" else "mostly books its own"
        found.append(f"{lead}: {conflict['detail']}")
    return found


def plan(
    arenas,
    shows,
    as_of,
    max_pitches=10,
    already_pitched=(),
    include_shows=(),
    previous=None,
    hard_nos=(),
    report_path="",
):
    """Balance verified shows across arenas; return picks, ladder, exclusions, gaps and state."""
    today = _day(as_of, "as_of")
    report_path = report_path_of(report_path)
    if isinstance(max_pitches, bool) or not isinstance(max_pitches, int):
        raise ValueError("max_pitches must be 3-20")
    if not 3 <= max_pitches <= 20:
        raise ValueError("max_pitches must be 3-20")
    for item in hard_nos:
        if item not in HARD_NOS:
            raise ValueError(f"hard no must be one of {', '.join(HARD_NOS)}")
    if not isinstance(arenas, list) or not 1 <= len(arenas) <= 8:
        raise ValueError("declare 1-8 arenas")
    for arena in arenas:
        check_arena(arena)
    order = [arena["id"] for arena in arenas]
    if len(set(order)) != len(order):
        raise ValueError("arena ids must be unique")
    for show in shows:
        check_show(show, set(order))

    # One record per show. A show found for two arenas keeps its best-ranked arena and every
    # method that found it.
    merged, duplicates = {}, 0
    for show in shows:
        key = slug(show["name"])
        # A pause or a conflict lowers the odds by one level: a likely show becomes a long shot.
        cautions = cautions_of(show, today)
        tier = show.get("tier", "likely")
        row = dict(
            show,
            key=key,
            tier="reach" if cautions else tier,
            cautions=cautions,
            found_by=sorted(set(show["found_by"])),
        )
        known = merged.get(key)
        if known is None:
            merged[key] = row
            continue
        duplicates += 1
        keep = (
            row
            if (row["rank"], order.index(row["arena"]))
            < (
                known["rank"],
                order.index(known["arena"]),
            )
            else known
        )
        keep["found_by"] = sorted(set(known["found_by"]) | set(row["found_by"]))
        merged[key] = keep

    skipped = {slug(n) for n in already_pitched if isinstance(n, str) and n.strip()}
    earlier = (previous or {}).get("shows", {})
    eligible = {arena: [] for arena in order}
    ladder, excluded = [], []
    for key, row in merged.items():
        reason = ""
        prior = earlier.get(key)
        paid = row.get("vendor_paid_only")
        if row.get("takes_guests") is False:
            reason = "does not host guests"
        elif paid:
            reason = f"a company's founder gets on only as a paid sponsor: {paid['terms']} ({paid['source']})"
        elif row["last_episode"] is None:
            reason = "latest episode date not verified"
        elif (today - _day(row["last_episode"], "last_episode")).days > ACTIVE_DAYS:
            reason = f"no episode since {row['last_episode']}"
        elif key in skipped:
            reason = "you already pitched it"
        elif prior and prior.get("sent"):
            reason = f"pitched on {prior['sent']} ({prior['report']})"
        elif row["route"] in ("direct_message", "contact_email") and "no_cold_email" in hard_nos:
            reason = (
                "your growth plan rules out cold email, and the show publishes no way to pitch it"
            )
        if prior and not prior.get("sent"):
            # Drafted before but never marked sent: still a candidate, after the new shows.
            row["drafted_before"] = {"date": prior["first_drafted"], "report": prior["report"]}
        if reason:
            excluded.append(dict(row, reason=reason))
        elif row["route"] == "ladder":
            ladder.append(row)
        else:
            eligible[row["arena"]].append(row)

    # New shows before ones an earlier report drafted, and clean shows before cautioned ones.
    for rows in eligible.values():
        rows.sort(key=lambda r: ("drafted_before" in r, bool(r["cautions"]), r["rank"], r["key"]))

    def round_robin(pools, limit):
        taken, depth = [], 0
        while len(taken) < limit and any(len(rows) > depth for rows in pools.values()):
            for arena in order:
                rows = pools[arena]
                if len(rows) > depth and len(taken) < limit:
                    taken.append(rows[depth])
            depth += 1
        return taken

    # Long shots get their own share, so a strong list of likely shows never crowds them out
    # and they never crowd out the likely ones. Likely shows come first in the report.
    reach = {arena: [r for r in eligible[arena] if r["tier"] == "reach"] for arena in order}
    likely = {arena: [r for r in eligible[arena] if r["tier"] == "likely"] for arena in order}
    reach_picks = round_robin(reach, max(1, round(max_pitches * REACH_SHARE)))
    picks = round_robin(likely, max_pitches - len(reach_picks)) + reach_picks
    chosen = {row["key"] for row in picks}
    for arena in order:
        for row in eligible[arena]:
            if row["key"] not in chosen:
                reason = (
                    "more long shots than one run tries; a later run can pick it"
                    if row["tier"] == "reach"
                    else "more shows than the pitches you asked for; a later run can pick it"
                )
                excluded.append(dict(row, reason=reason))
    ladder.sort(
        key=lambda r: (r.get("deadline") or "9999-12-31", order.index(r["arena"]), r["rank"])
    )

    outcomes = []
    for name in include_shows:
        if not isinstance(name, str) or not name.strip():
            continue
        key = slug(name)
        if key in chosen:
            outcome = "picked"
        elif any(row["key"] == key for row in ladder):
            outcome = "ladder"
        else:
            hit = next((row for row in excluded if row["key"] == key), None)
            outcome = f"left out: {hit['reason']}" if hit else "not evaluated"
        outcomes.append({"name": name.strip(), "outcome": outcome})

    # Gaps are read by the founder: name audiences by their label, never by id or method.
    gaps = []
    kinds = {arena["kind"] for arena in arenas}
    if "buyers" not in kinds:
        gaps.append("No shows your buyers listen to were searched.")
    if "craft" not in kinds:
        gaps.append("No shows for people in your own line of work were searched.")
    tried = {arena["id"]: set(arena.get("tried", [])) for arena in arenas}
    labels = {arena["id"]: arena["label"] for arena in arenas}
    for arena in order:
        rows = [row for row in merged.values() if row["arena"] == arena]
        methods = tried[arena] | {method for row in rows for method in row["found_by"]}
        if not rows:
            gaps.append(f"{labels[arena]}: no shows found.")
        if len(methods) < 2:
            gaps.append(f"{labels[arena]}: searched only one way, so shows may be missing.")
        if rows and not any(row["key"] in chosen for row in rows):
            gaps.append(f"{labels[arena]}: shows were found, but none made the picks this run.")
    for item in outcomes:
        if item["outcome"] == "not evaluated":
            gaps.append(f"You asked about {item['name']}, and it was not checked.")

    count = len(picks)
    verdict = "fit" if count >= FIT_MIN_PICKS else "thin" if count else "not a fit"
    # The state records drafts. A show counts as pitched only once `sent` is set, by the send
    # step or by the founder listing it in already_pitched.
    state = {key: dict(item) for key, item in earlier.items()}
    for key in skipped & set(state):
        state[key]["sent"] = state[key]["sent"] or as_of
    for row in picks:
        if row["key"] in state:
            continue
        state[row["key"]] = {
            "name": row["name"],
            "route": row["route"],
            "first_drafted": as_of,
            "sent": None,
            "report": report_path,
        }
    if len(state) > MAX_STATE_SHOWS:
        newest = sorted(state.items(), key=lambda kv: kv[1]["first_drafted"], reverse=True)
        state = dict(newest[:MAX_STATE_SHOWS])
    return {
        "status": "complete" if count else "no shows verified",
        "verdict": verdict,
        "picks": picks,
        "ladder": ladder,
        "excluded": excluded,
        "include_outcomes": outcomes,
        "gaps": gaps,
        "duplicates_merged": duplicates,
        "state": {"version": 2, "shows": state},
    }


def pitch_block(picks, drafts):
    """The emails stage two may send: one per published_email pick, nothing else."""
    wanted = {row["key"]: row for row in picks if row["route"] == "published_email"}
    if not isinstance(drafts, list):
        raise ValueError("drafts must be a list")
    block, seen = [], set()
    for draft in drafts:
        if not isinstance(draft, dict):
            raise ValueError("each draft must be an object")
        key = slug(draft.get("show"))
        row = wanted.get(key)
        if row is None:
            raise ValueError(f"{draft.get('show')} is not a published_email pick")
        if key in seen:
            raise ValueError(f"{row['name']} has two drafts")
        seen.add(key)
        block.append(
            {
                "show": row["name"],
                "to": row["pitch_address"],
                "address_source": row["address_source"],
                "language": row["language"],
                "subject": _text(draft.get("subject"), "subject", 120),
                "body": _text(draft.get("body"), "body", 3000),
                "follow_up": _text(draft.get("follow_up"), "follow_up", 1200),
            }
        )
    missing = sorted(wanted[key]["name"] for key in set(wanted) - seen)
    if missing:
        raise ValueError(f"no draft for {', '.join(missing)}")
    return {"version": 1, "pitches": block}


MAX_FETCH_BYTES = 5_000_000
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
LINK = re.compile(r"\]\((https?://[^)\s]+)\)|<(https?://[^>\s]+)>|(?<![(<\w])(https?://[^\s)>\]]+)")


def _get(url):
    """One public GET through the run's proxy (urllib reads HTTPS_PROXY). (status, bytes)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(_url(url, "url"), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, response.read(MAX_FETCH_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def read_feed(url, fetch=None):
    """A show's RSS feed: its newest episode dates and the address its owner gives.

    Returns {"status", "episode_dates" (newest first, up to five), "owner_email"}. The
    address is None when the feed names none or only a hosting or template address.
    """
    from email.utils import parsedate_to_datetime
    from xml.etree import ElementTree

    status, body = (fetch or _get)(url)
    result = {"status": status, "episode_dates": [], "owner_email": None}
    # A feed is untrusted: refuse entity declarations (entity expansion) rather than parse them.
    if not 200 <= status < 300 or not body or b"<!ENTITY" in body.upper():
        return result
    try:
        channel = ElementTree.fromstring(body).find("channel")
    except ElementTree.ParseError:
        return result
    if channel is None:
        return result
    days = set()
    for item in channel.findall("item"):
        try:
            days.add(parsedate_to_datetime(item.findtext("pubDate", "").strip()).date())
        except (TypeError, ValueError, IndexError):
            continue
    result["episode_dates"] = [day.isoformat() for day in sorted(days, reverse=True)[:5]]
    for raw in (
        channel.findtext(f"{ITUNES}owner/{ITUNES}email"),
        channel.findtext(f"{ITUNES}email"),
        channel.findtext("managingEditor"),
    ):
        address = (raw or "").strip().split(" ")[0]
        if EMAIL.fullmatch(address) and not PLACEHOLDER_EMAIL.search(address):
            result["owner_email"] = address
            break
    return result


def check_links(text, fetch=None):
    """Open every link in the report. Returns {"broken": [...], "unchecked": [...]}.

    broken: the page answered 404 or 410, or nothing answered. unchecked: the site refused
    a script (401, 403, 429 or a 5xx); keep those links. Every other answer is a live page.
    """
    urls = []
    for match in LINK.finditer(text):
        url = next(group for group in match.groups() if group).rstrip(".,;:")
        if url not in urls:
            urls.append(url)
    broken, unchecked = [], []
    for url in urls:
        status, _ = (fetch or _get)(url)
        if status in (0, 404, 410):
            broken.append(url)
        elif status in (401, 403, 429) or status >= 500:
            unchecked.append(url)
    return {"broken": broken, "unchecked": unchecked}


LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")


def quote(text):
    """A draft as a Markdown blockquote that reads exactly as written.

    Markdown joins consecutive lines into one paragraph, so each line that another line
    follows ends in a hard break, a list is set off by empty quote lines, and every line,
    the sign-off and its link included, stays inside the quote.
    """
    lines = _text(text, "draft", 6000).replace("\r\n", "\n").split("\n")
    out = []
    for index, line in enumerate(lines):
        line = line.rstrip()
        if not line:
            if out and out[-1] != ">":
                out.append(">")
            continue
        item = bool(LIST_ITEM.match(line))
        if out and out[-1] != ">":
            previous_item = bool(LIST_ITEM.match(out[-1][2:]))
            if item != previous_item:
                out[-1] = out[-1].removesuffix("\\")
                out.append(">")
        following = lines[index + 1].rstrip() if index + 1 < len(lines) else ""
        joins = following and not item and not LIST_ITEM.match(following)
        out.append("> " + line + ("\\" if joins else ""))
    while out and out[-1] == ">":
        out.pop()
    return "\n".join(out)
```

Pass `already_pitched` and `include_shows` as lists of the names in those inputs (split on
commas and new lines), `hard_nos` as the ids of the hard no's the growth plan records (`no
cold email` is `no_cold_email`), and `report_path` as the declared output path of this run.
