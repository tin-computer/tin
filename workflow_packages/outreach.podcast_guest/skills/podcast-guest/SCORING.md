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
- `found_by`: the methods that surfaced it, from `METHODS`.
- `similar_guest`: `{"name", "episode", "url", "date"}` for a past guest who resembles the
  founder in this arena, or `null`. `url` is the episode's page, or the show's page when its
  episodes have none.
- `takes_guests`: `false` for a show that only has its hosts talking. Optional; a show with
  `false` is listed as excluded.
- `route`: `published_email` (the show's own page or feed asks for guest pitches at an
  address; a host's general or personal contact address is `direct_message`),
  `guest_form`, `direct_message` (writing to a host with no stated intake), `warm_intro`
  (someone the founder knows is connected) or `ladder` (a step comes first: a talk, a guest
  post, a launch).
- `rank`: your rank for it within its arena, 1 first.
- `reason`: why it fits, in one line.
- `pitch_address` and `address_source`: for `published_email`, the address and the page that
  publishes it. Never guessed, never Podscan's `listed_email` alone.
- `ladder_step` and `deadline`: for `ladder`, the step and its date if it has one.
- `notes`: anything the founder should weigh: a guest fee or sponsored-episode signs, a
  conflict, a data caveat. Optional. Notes never exclude a show.

## Memory between runs

Every report ends with a `tin-podcast-state` block. Read the block of every earlier report in
the output folder, pass each parsed value to `read_state()`, and merge the trusted ones with
`merge_states()`. A block that fails `read_state()` is ignored, and the report says so.

```python
import re
import unicodedata
from datetime import date

KINDS = ("buyers", "craft", "home", "topic", "other")
METHODS = (
    "lookalike_company",
    "lookalike_title",
    "buyer_guests",
    "transcript_topic",
    "country_chart",
    "local_youtube",
    "named_show",
    "web_search",
)
ROUTES = ("published_email", "guest_form", "direct_message", "warm_intro", "ladder")
HARD_NOS = (
    "no_cold_email",
    "no_paid_ads",
    "no_founder_posting",
    "no_discounting",
    "no_unbacked_claims",
)
# A show with no episode in this window is not booking guests now.
ACTIVE_DAYS = 120
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
    route = show.get("route")
    if route not in ROUTES:
        raise ValueError(f"route must be one of {', '.join(ROUTES)}")
    rank = show.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("rank must be a whole number from 1")
    _text(show.get("reason"), "reason", 400)
    if route == "published_email":
        if not isinstance(show.get("pitch_address"), str) or not EMAIL.fullmatch(
            show["pitch_address"]
        ):
            raise ValueError("published_email needs the pitch_address the show publishes")
        _url(show.get("address_source"), "address_source")
    elif show.get("pitch_address") is not None:
        raise ValueError("only published_email shows carry a pitch_address")
    if route == "ladder":
        _text(show.get("ladder_step"), "ladder_step", 400)
    if show.get("deadline") is not None:
        _day(show["deadline"], "deadline")
    if show.get("notes") is not None:
        _text(show["notes"], "notes", 600)
    if show.get("takes_guests", True) is not True and show.get("takes_guests") is not False:
        raise ValueError("takes_guests must be true or false")


def read_state(value):
    """Validate one earlier report's state block. Returns None when it cannot be trusted."""
    try:
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        shows = value.get("shows")
        if not isinstance(shows, dict) or len(shows) > MAX_STATE_SHOWS:
            return None
        clean = {}
        for key, item in shows.items():
            if not isinstance(key, str) or key != slug(item.get("name")):
                return None
            if item.get("route") not in ROUTES[:-1]:
                return None
            _day(item.get("first_pitched"), "first_pitched")
            report = item.get("report")
            if not isinstance(report, str) or not report.strip():
                return None
            clean[key] = {
                "name": item["name"],
                "route": item["route"],
                "first_pitched": item["first_pitched"],
                "report": report,
            }
        return {"version": 1, "shows": clean}
    except (AttributeError, TypeError, ValueError):
        return None


def merge_states(states):
    """Union trusted earlier states; a show keeps the report that first pitched it."""
    merged = {}
    for state in states:
        if state is None:
            continue
        for key, item in state["shows"].items():
            known = merged.get(key)
            if known is None or item["first_pitched"] < known["first_pitched"]:
                merged[key] = dict(item)
    return {"version": 1, "shows": merged}


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
        row = dict(show, key=key, found_by=sorted(set(show["found_by"])))
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
        if row.get("takes_guests") is False:
            reason = "does not host guests"
        elif row["last_episode"] is None:
            reason = "latest episode date not verified"
        elif (today - _day(row["last_episode"], "last_episode")).days > ACTIVE_DAYS:
            reason = f"no episode since {row['last_episode']}"
        elif key in skipped:
            reason = "named in already_pitched"
        elif key in earlier:
            prior = earlier[key]
            reason = f"pitched on {prior['first_pitched']} in {prior['report']}"
        elif row["route"] == "direct_message" and "no_cold_email" in hard_nos:
            reason = "hard no: no cold email (the show states no way to pitch it)"
        if reason:
            excluded.append(dict(row, reason=reason))
        elif row["route"] == "ladder":
            ladder.append(row)
        else:
            eligible[row["arena"]].append(row)

    for rows in eligible.values():
        rows.sort(key=lambda r: (r["rank"], r["key"]))
    picks, depth = [], 0
    while len(picks) < max_pitches and any(len(rows) > depth for rows in eligible.values()):
        for arena in order:
            rows = eligible[arena]
            if len(rows) > depth and len(picks) < max_pitches:
                picks.append(rows[depth])
        depth += 1
    chosen = {row["key"] for row in picks}
    for arena in order:
        for row in eligible[arena]:
            if row["key"] not in chosen:
                excluded.append(dict(row, reason="over max_pitches; eligible next run"))
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
            outcome = f"excluded: {hit['reason']}" if hit else "not evaluated"
        outcomes.append({"name": name.strip(), "outcome": outcome})

    gaps = []
    kinds = {arena["kind"] for arena in arenas}
    for kind in ("buyers", "craft"):
        if kind not in kinds:
            gaps.append(f"no {kind} arena was searched")
    tried = {arena["id"]: set(arena.get("tried", [])) for arena in arenas}
    for arena in order:
        rows = [row for row in merged.values() if row["arena"] == arena]
        methods = tried[arena] | {method for row in rows for method in row["found_by"]}
        if not rows:
            gaps.append(f"{arena}: no candidates found")
        if len(methods) < 2:
            used = ", ".join(sorted(methods)) or "none recorded"
            gaps.append(f"{arena}: fewer than two methods tried ({used})")
        if rows and not any(row["key"] in chosen for row in rows):
            gaps.append(f"{arena}: no pick this run")
    for item in outcomes:
        if item["outcome"] == "not evaluated":
            gaps.append(f"asked-for show not evaluated: {item['name']}")

    count = len(picks)
    verdict = "fit" if count >= FIT_MIN_PICKS else "thin" if count else "not a fit"
    state = {key: dict(item) for key, item in earlier.items()}
    for row in picks:
        state[row["key"]] = {
            "name": row["name"],
            "route": row["route"],
            "first_pitched": as_of,
            "report": report_path,
        }
    if len(state) > MAX_STATE_SHOWS:
        newest = sorted(state.items(), key=lambda kv: kv[1]["first_pitched"], reverse=True)
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
        "state": {"version": 1, "shows": state},
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
```

Pass `already_pitched` and `include_shows` as lists of the names in those inputs (split on
commas and new lines), `hard_nos` as the ids of the hard no's the growth plan records (`no
cold email` is `no_cold_email`), and `report_path` as the declared output path of this run.
