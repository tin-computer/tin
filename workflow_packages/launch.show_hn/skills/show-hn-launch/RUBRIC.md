# Readiness, timing and run-to-run memory

Extract the single Python block below into a scratch module and use it unchanged. You gather the
evidence for the launch (what it points to, whether a stranger can try it, the technical story,
how novel it is, how honestly it is framed, and whether the founder will be present). This code
decides what blocks a launch, the readiness score and verdict, the concrete posting window, and
which targets a later run must not re-post too soon. Never score, gate or pick a time by hand.

## The launch record

Fill one record for this launch, every field from the evidence you actually read:

- `target_url`: the exact `https://` link the Show HN would point people to — the live product,
  demo or repository they can open. Never a link behind a login.
- `access`: how much a first-time visitor has to do before they see the thing work.
  - `instant`: they can use it or read the code with no account and no payment.
  - `light_signup`: an optional or email-only step, or a repo that builds in a minute.
  - `walled`: a required signup, waitlist, sales call or payment before anything works.
- `founder_present`: `true` only if the founder will sit with the thread and reply for the whole
  launch day. Hacker News rewards presence more than polish; an absent founder is a wasted launch.
- `technical_story`: 2 when there is a genuine "how we built it / why it is hard" story with real
  detail, 1 when there is some, 0 when there is none.
- `novelty`: 2 when the approach is new or does something incumbents do not, 1 when it is an
  incremental take, 0 when it is a me-too of a tool the audience already uses.
- `honesty`: 2 when the framing states real limitations plainly, 1 when it is partly candid,
  0 when it is only marketing language. HN punishes hype fast.
- `title`: the drafted title, which must read `Show HN: <plain description>` — at most 80
  characters, no trailing year, no ALL-CAPS words and no superlatives ("best", "revolutionary").
- `major_change_since_last`: `true` only if this is a re-launch of a target launched before and
  it has changed substantially since. A substantially changed project may be shown again; a
  cosmetic change may not.

## Memory between runs

Every report ends with a `tin-show-hn-state` block. Read the block of every earlier report in the
output folder, pass each parsed value to `read_state()`, and merge the trusted ones with
`merge_states()`. A block that fails `read_state()` is ignored, and the report says so. The code
uses this to refuse a too-soon repost of the same target.

```python
import re
import unicodedata
from datetime import date, timedelta

ACCESS = {"instant": 2, "light_signup": 1, "walled": 0}
HARD_NOS = ("no_founder_posting", "no_paid_ads", "no_unbacked_claims", "no_discounting")
SCORED = ("technical_story", "novelty", "honesty")
REQUIRED = (
    "target_url",
    "access",
    "founder_present",
    "technical_story",
    "novelty",
    "honesty",
    "title",
    "major_change_since_last",
)
MAX_SCORE = 8  # access + technical_story + novelty + honesty, 0..2 each
READY_MIN = 6
ALMOST_MIN = 4
REPOST_COOLDOWN_DAYS = 365  # HN allows a repost of an unchanged target after about a year
PREFERRED_WEEKDAYS = (1, 2, 3)  # Tue, Wed, Thu; avoid Mon, Fri and weekends
POST_WINDOW_UTC = "13:00-15:00 UTC"  # ~08:00-10:00 America/New_York, the HN front-page window
MAX_STATE_TARGETS = 500
_TITLE_PREFIX = "Show HN:"


def _day(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error


def target_slug(url):
    """Normalise a URL to one target key: no scheme, no www, no trailing slash, lower case."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("target_url must be a non-empty https link")
    text = url.strip().lower()
    if not text.startswith("https://"):
        raise ValueError("target_url must be an https link a stranger can open")
    text = text[len("https://"):]
    text = re.sub(r"^www\.", "", text)
    text = text.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    if not text:
        raise ValueError("target_url has no host")
    return text


def _check_title(title):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be non-empty text")
    if not title.startswith(_TITLE_PREFIX) or not title[len(_TITLE_PREFIX):].strip():
        raise ValueError('title must read "Show HN: <description>"')
    if len(title) > 80:
        raise ValueError("title must be at most 80 characters")
    if re.search(r"\b(19|20)\d{2}$", title.strip()):
        raise ValueError("title must not end with a year")
    words = re.findall(r"[A-Za-z]{2,}", title[len(_TITLE_PREFIX):])
    if any(word.isupper() for word in words):
        raise ValueError("title must not shout in ALL CAPS")
    banned = {"best", "revolutionary", "ultimate", "amazing", "world-class", "cutting-edge"}
    if {word.lower() for word in words} & banned:
        raise ValueError("title must not use superlatives HN readers distrust")


def check(record):
    missing = [field for field in REQUIRED if field not in record]
    if missing:
        raise ValueError(f"launch record is missing {', '.join(missing)}")
    target_slug(record["target_url"])
    if record["access"] not in ACCESS:
        raise ValueError(f"access must be one of {', '.join(ACCESS)}")
    for field in ("founder_present", "major_change_since_last"):
        if not isinstance(record[field], bool):
            raise ValueError(f"{field} must be true or false")
    for field in SCORED:
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2:
            raise ValueError(f"{field} must be 0, 1 or 2")
    _check_title(record["title"])


def read_state(value):
    """Validate one earlier report's state block. Returns None when it cannot be trusted."""
    try:
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        targets = value.get("targets")
        if not isinstance(targets, dict) or len(targets) > MAX_STATE_TARGETS:
            return None
        clean = {}
        for key, item in targets.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(item, dict):
                return None
            url = item.get("url")
            target_slug(url)
            _day(item.get("prepared"), "prepared")
            report = item.get("report")
            if not isinstance(report, str) or not report.strip():
                return None
            clean[key] = {"url": url, "prepared": item["prepared"], "report": report}
        return {"version": 1, "targets": clean}
    except (AttributeError, TypeError, ValueError):
        return None


def merge_states(states):
    """Union trusted earlier states; a target keeps the most recent time it was prepared."""
    merged = {}
    for state in states:
        if state is None:
            continue
        for key, item in state["targets"].items():
            known = merged.get(key)
            if known is None or item["prepared"] > known["prepared"]:
                merged[key] = dict(item)
    return {"version": 1, "targets": merged}


def recommend_window(start):
    """First Tue/Wed/Thu on or after `start`, with the HN front-page posting window."""
    day = start
    for _ in range(7):
        if day.weekday() in PREFERRED_WEEKDAYS:
            return {"date": day.isoformat(), "window": POST_WINDOW_UTC}
        day += timedelta(days=1)
    raise ValueError("no preferred weekday found")  # unreachable within a week


def assess(
    record,
    as_of,
    earliest_available="",
    past_targets=(),
    previous=None,
    hard_nos=(),
    report_path="",
):
    """Gate, score and time one launch; return the verdict, window, blockers and next state."""
    today = _day(as_of, "as_of")
    check(record)
    for item in hard_nos:
        if item not in HARD_NOS:
            raise ValueError(f"hard no must be one of {', '.join(HARD_NOS)}")
    start = _day(earliest_available, "earliest_available") if earliest_available else today
    if start < today:
        start = today
    slug = target_slug(record["target_url"])
    skipped = {target_slug(u) for u in past_targets if isinstance(u, str) and u.strip()}
    known = (previous or {}).get("targets", {})

    blockers = []
    if "no_founder_posting" in hard_nos:
        blockers.append("hard no: no founder posting — the founder does not post in public")
    if record["access"] == "walled":
        blockers.append("walled: a Show HN must be usable without a signup, waitlist or payment")
    if not record["founder_present"]:
        blockers.append("absent: the founder must be free to reply in the thread all launch day")
    if not record["major_change_since_last"]:
        prior = known.get(slug)
        recent = None
        if slug in skipped:
            recent = "an earlier launch outside Tin"
        elif prior is not None:
            gap = (today - _day(prior["prepared"], "prepared")).days
            if gap < REPOST_COOLDOWN_DAYS:
                until = (_day(prior["prepared"], "prepared")
                         + timedelta(days=REPOST_COOLDOWN_DAYS)).isoformat()
                recent = f"prepared on {prior['prepared']} in {prior['report']}; wait until {until}"
        if recent:
            blockers.append(f"repost too soon: {recent}, or re-launch only with major changes")

    access_score = ACCESS[record["access"]]
    score = access_score + sum(record[field] for field in SCORED)
    if blockers:
        verdict, status = "not yet", "hold"
    elif score >= READY_MIN and access_score >= 1 and record["novelty"] >= 1 \
            and record["honesty"] >= 1:
        verdict, status = "ready", "ready"
    elif score >= ALMOST_MIN:
        verdict, status = "almost", "almost"
    else:
        verdict, status = "not yet", "not ready"

    window = recommend_window(start) if verdict != "not yet" else None

    state = {key: dict(item) for key, item in known.items()}
    state[slug] = {"url": record["target_url"], "prepared": as_of, "report": report_path}
    if len(state) > MAX_STATE_TARGETS:
        newest = sorted(state.items(), key=lambda kv: kv[1]["prepared"], reverse=True)
        state = dict(newest[:MAX_STATE_TARGETS])

    return {
        "status": status,
        "verdict": verdict,
        "score": score,
        "max_score": MAX_SCORE,
        "window": window,
        "blockers": blockers,
        "state": {"version": 1, "targets": state},
    }
```

Pass `earliest_available` as the input date the founder can sit with the thread (empty means from
today), `past_targets` as the URLs in the input plus any from earlier reports' state, `hard_nos`
as the ids the growth plan records (`no founder posting` is `no_founder_posting`), and
`report_path` as this run's declared output path. Use the verdict, score, window, blockers and
state exactly as returned; if you disagree with a result, say why next to it, do not change it.
