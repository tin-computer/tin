"""Did a dated change move a daily metric? One weekly process behaviour chart per change.

Days are grouped into 7-day weeks that start on each change date. The weeks before a change set
its natural process limits (Wheeler's XmR chart); a metric already on a clear trend is charted
around that trend, with limits that widen as the projection extends. The first four weeks after
the change decide the verdict. Everything here is deterministic arithmetic; no model is called.
"""

import csv
import io
import math
import re
import statistics
from datetime import date, timedelta
from itertools import pairwise

PATH = "reports/CHANGE_CHECK.md"
MIN_WEEKS, MAX_WEEKS, AFTER = 10, 12, 4
WINDOW = timedelta(days=7 * AFTER)
MAX_DAYS, LARGEST = 1000, 1e12
EARLIEST, LATEST = date(1900, 1, 1), date(2999, 12, 31)
SHOWN, MAX_CHANGES, MAX_LINE = 10, 10, 240
# Wheeler's XmR scaling (3 / 1.128): natural process limits sit this many average moving ranges
# from the central line.
SCALE = 2.66
# Two-sided 80% Student's t for 8-10 degrees of freedom: a baseline slope this clear is a trend.
TREND_T = {8: 1.397, 9: 1.383, 10: 1.372}
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
ACTIONS = {
    "Moved up": "Keep and scale it if a rise is good; check nothing else changed then.",
    "Moved down": "Investigate or revert it if a drop is bad; check nothing else changed then.",
    "One-off spike": "Don't count it as a lasting change.",
    "One-off dip": "Don't count it as a lasting change.",
    "No detectable change": "Don't credit it with a lift; keep or drop it for other reasons.",
    "Can't separate": "List only the first of these changes to judge them together.",
}


def run(ctx, inputs):
    problems, notes = [], []
    rows = parse_rows(inputs["series_csv"], problems)
    zero_fill = inputs.get("missing_days_are_zero", False)
    values = inspect_series(rows, zero_fill, run_day(ctx), problems, notes)
    changes = read_changes(inputs["changes"], problems)
    title = f"# Did it move? {cell(inputs['metric_name'])}"
    lines = diagnostic(title, problems) if problems else report(title, values, changes, notes)
    return {"path": PATH, "content": "\n".join(lines) + "\n"}


# Reading and inspecting the input


def day_of(text):
    text = text.strip()
    if not ISO_DATE.fullmatch(text):
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    return day if EARLIEST <= day <= LATEST else None


def run_day(ctx):
    try:
        return day_of(str(ctx["created_at"])[:10])
    except (KeyError, TypeError):
        return None


def number(text):
    try:
        return float(text.strip())
    except ValueError:
        return math.nan


def clip(text, size=60):
    text = " ".join(str(text).split()).replace("`", "'")
    return text if len(text) <= size else text[: size - 1] + "…"


def cell(text):
    """One line of Markdown table text: backslashes and pipes are escaped, whitespace collapsed."""
    return " ".join(str(text).split()).replace("\\", "\\\\").replace("|", "\\|")


def parse_rows(raw, problems):
    """Read `date,value` rows; a first row with no digits is a header."""
    reader = csv.reader(io.StringIO(raw.lstrip("\ufeff"), newline=""))
    rows = [row for row in reader if any(part.strip() for part in row)]
    if rows and not any(character.isdigit() for character in rows[0][0]):
        if len(rows[0]) != 2:
            message = "Use exactly two columns separated by a comma, like `date,value`."
            problems.append((message, []))
            return []
        rows = rows[1:]
    parsed, unreadable = [], []
    for row in rows:
        shown = f"`{clip(','.join(row))}`"
        if len(row) != 2:
            unreadable.append(f"{shown} needs exactly two columns (remove thousands separators)")
        elif (day := day_of(row[0])) is None:
            unreadable.append(f"{shown} needs a YYYY-MM-DD date between 1900 and 2999")
        elif not abs(amount := number(row[1])) <= LARGEST:
            unreadable.append(f"{shown} needs a number between -1e12 and 1e12")
        else:
            parsed.append((day, amount))
    if unreadable:
        problems.append(("Some rows can't be read:", unreadable))
    return parsed


def inspect_series(rows, zero_fill, today, problems, notes):
    values, repeated = {}, []
    for day, amount in rows:
        if day in values:
            repeated.append(str(day))
        else:
            values[day] = amount
    if repeated:
        problems.append(("Some dates appear more than once; keep one row per day:", repeated))
    if not values:
        if not problems:
            problems.append(("Paste at least a few months of daily values.", []))
        return values
    last = max(values)
    if today and len(values) > 1 and last >= today - timedelta(days=1):
        del values[last]
        notes.append(f"Left out {last}: it is within a day of this run, so it may be incomplete.")
    first, last = min(values), max(values)
    span = (last - first).days + 1
    if span > MAX_DAYS:
        message = f"The data covers {span:,} days. Paste at most the most recent {MAX_DAYS:,}."
        problems.append((message, []))
        return values
    missing = [first + timedelta(days=offset) for offset in range(span)]
    missing = [day for day in missing if day not in values]
    if missing and zero_fill:
        values.update(dict.fromkeys(missing, 0.0))
        notes.append(f"Counted {len(missing)} missing days as zero.")
    elif missing:
        message = (
            "Some days are missing. Add them (with 0 if nothing happened) "
            "or turn on Count missing days as zero:"
        )
        problems.append((message, spans(missing)))
    steps = list(pairwise(values[day] for day in sorted(values)))
    rises = sum(later > earlier for earlier, later in steps)
    if len(steps) >= 27 and all(later >= earlier for earlier, later in steps):
        if 2 * rises >= len(steps):
            message = (
                "The values never go down, which looks like a running total. Paste the daily "
                "amounts instead (each day minus the day before); for a level such as MRR, "
                "paste its daily change."
            )
            problems.append((message, []))
    return values


def spans(days):
    """Consecutive dates as ranges, like `2026-01-04 to 2026-01-09`."""
    ranges, start = [], days[0]
    for previous, day in pairwise([*days, None]):
        if day != previous + timedelta(days=1):
            ranges.append(str(start) if start == previous else f"{start} to {previous}")
            start = day
    return ranges


def read_changes(text, problems):
    """One change per line of the change-log textarea; blank lines are ignored."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > MAX_CHANGES:
        message = f"List at most {MAX_CHANGES} changes, one per line; this has {len(lines)}."
        problems.append((message, []))
        return []
    changes, unreadable, long, shared, seen = [], [], [], [], set()
    for line in lines:
        head, bar, tail = line.partition("|")
        day, what = day_of(head), " ".join(tail.split())
        if len(line) > MAX_LINE:
            long.append(f"`{clip(line)}`")
        elif not bar or day is None or not what:
            unreadable.append(f"`{clip(line)}`")
        elif day in seen:
            shared.append(str(day))
        else:
            seen.add(day)
            changes.append((day, what))
    if long:
        problems.append((f"Keep each change line to {MAX_LINE} characters:", long))
    if unreadable:
        message = "Write each change as `YYYY-MM-DD | what changed`, dated 1900 to 2999:"
        problems.append((message, unreadable))
    if shared:
        problems.append(("Two changes share a date; combine them into one line:", shared))
    return sorted(changes)


def diagnostic(title, problems):
    lines = [title, "", "Nothing was judged. Fix the input below and run it again.", ""]
    for message, items in problems:
        lines.append(f"- {message}")
        lines.extend(f"  - {item}" for item in items[:SHOWN])
        if len(items) > SHOWN:
            lines.append(f"  - …and {len(items) - SHOWN:,} more")
    return lines


# The chart


class Chart:
    """Natural process limits from the weeks before a change: flat, or around a clear trend."""

    def __init__(self, levels, nonnegative):
        positive = all(level > 0 for level in levels)
        ys = [math.log(level) for level in levels] if positive else list(levels)
        self.n, xs = len(ys), list(range(len(ys)))
        slope, intercept = statistics.linear_regression(xs, ys)
        residuals = [y - intercept - slope * x for x, y in zip(xs, ys, strict=True)]
        self.middle = (self.n - 1) / 2
        self.sxx = sum((x - self.middle) ** 2 for x in xs)
        spread = sum(error * error for error in residuals) / (self.n - 2)
        if spread > 0:
            clear = abs(slope) / math.sqrt(spread / self.sxx)
        else:
            clear = math.inf if slope else 0.0
        self.trend = clear >= TREND_T[self.n - 2]
        # A trend on positive weeks is charted on log values, so growth compounds. A flat chart is
        # Wheeler's XmR on the weekly levels themselves.
        self.logged = self.trend and positive
        if self.trend:
            self.slope, self.intercept = slope, intercept
            self.width, typical = moving_range(residuals), statistics.fmean(ys)
        else:
            self.center = statistics.fmean(levels)
            self.width, typical = moving_range(levels), self.center
        self.varies = self.width > 1e-9 * max(1.0, abs(typical))
        self.nonnegative = nonnegative
        # Print enough decimals to resolve the range's half-width to about two digits.
        half = SCALE * self.width
        if self.logged:
            half = min(levels) * (math.exp(min(half, 700.0)) - 1)
        self.decimals = decimals_for(half)

    def bounds(self, x):
        """Lower limit, lower midpoint, centre, upper midpoint, upper limit, in chart units."""
        center, widen = (self.intercept + self.slope * x, 1.0) if self.trend else (self.center, 1.0)
        if self.trend and x >= self.n:
            # Beyond the baseline, add the uncertainty of the fitted line itself.
            widen = math.sqrt(1 + 1 / self.n + (x - self.middle) ** 2 / self.sxx)
        half = SCALE * self.width * widen
        low = center - half
        if not self.logged and self.nonnegative and low < 0:
            low = -math.inf
        return low, center - half / 2, center, center + half / 2, center + half

    def to_chart(self, level):
        if not self.logged:
            return level
        return math.log(level) if level > 0 else -math.inf

    def to_level(self, value):
        return math.exp(min(value, 700.0)) if self.logged else value

    def span_text(self, x):
        low, _, _, _, high = self.bounds(x)
        top = fmt(self.to_level(high), self.decimals)
        if low == -math.inf:
            return f"up to {top}"
        return f"{fmt(self.to_level(low), self.decimals)} to {top}"

    def smallest_move(self):
        low, _, center, _, high = self.bounds(self.n)
        if self.logged:
            up, down = math.exp(min(high - center, 700.0)) - 1, 1 - math.exp(low - center)
            return f"{percent(up)} up or {percent(down)} down against the trend"
        share = f" ({percent((high - center) / abs(center))})" if not self.trend and center else ""
        return f"{fmt(high - center, self.decimals)} per day{share}"

    def describe(self, before):
        end = before[-1][0] + timedelta(days=6)
        weeks = f"the {len(before)} weeks from {before[0][0]} to {end}"
        if not self.trend:
            return (
                f"Normal range from {weeks}: average {fmt(self.center, self.decimals)} per day, "
                f"normal range {self.span_text(0)} per day."
            )
        if self.logged:
            pace = f"{signed_percent(math.exp(min(self.slope, 700.0)) - 1)} a week"
        else:
            sign = "+" if self.slope >= 0 else ""
            pace = f"{sign}{fmt(self.slope, self.decimals)} per day each week"
        return (
            f"In {weeks} the metric was already trending {pace}, so the expected range follows "
            "that trend and widens the further it projects."
        )


def moving_range(values):
    return statistics.fmean(abs(later - earlier) for earlier, later in pairwise(values))


def decimals_for(half):
    if not (math.isfinite(half) and half > 0):
        return 1
    return max(1, min(6, 1 - math.floor(math.log10(half))))


def fmt(value, decimals=1):
    # Inputs stay within ±1e12; only a far-projected trend limit can grow past 1e13.
    return f"{value:.3g}" if abs(value) >= 1e13 else f"{value:,.{decimals}f}"


def percent(share):
    if share < 0.01:
        return "under 1%"
    return f"{100 * share:.0f}%" if share < 1e4 else f"{100 * share:.3g}%"


def signed_percent(share):
    return f"{100 * share:+.1f}%" if abs(share) < 1e4 else f"{100 * share:+.3g}%"


def week_level(values, start):
    days = [values.get(start + timedelta(days=offset)) for offset in range(7)]
    return None if None in days else statistics.fmean(days)


# Judging each change


def judge_all(values, changes):
    nonnegative = all(value >= 0 for value in values.values())
    verdicts, since = [], None
    for index, (day, what) in enumerate(changes):
        after = changes[index + 1][0] if index + 1 < len(changes) else None
        result = judge(values, day, since, after, nonnegative)
        verdicts.append((day, what, result))
        since = restart(day, result) or since
    return verdicts


def restart(day, result):
    """Where later baselines may start; None only when this change left the metric alone."""
    label = result["label"]
    if result.get("swings"):
        # Breakouts on both sides are special weeks, not routine variation to learn from.
        why = f"{AFTER} weeks after the change on {day}, whose weeks swung both ways"
        return {"start": day + WINDOW, "change": day, "why": why}
    if label == "No detectable change":
        return None
    if label.startswith("Moved"):
        return {"start": day, "change": day, "why": f"when the change on {day} moved the metric"}
    if label.startswith("One-off"):
        week = result["special"]
        why = f"after the one-off week of {week}"
        return {"start": week + timedelta(days=7), "change": day, "why": why}
    why = f"{AFTER} weeks after the change on {day}, whose effect couldn't be judged"
    return {"start": day + WINDOW, "change": day, "why": why}


def judge(values, day, since, after, nonnegative):
    # One day past the fourth week, so a likely partial last day never cuts that week short.
    recheck = day + WINDOW + timedelta(days=1)
    blocked = after is not None and after < day + WINDOW
    if since and since["start"] > day:
        reason = (
            f"It came less than {AFTER} weeks after the change on {since['change']}, "
            "so their effects overlap."
        )
        return verdict("Can't separate", 0, ACTIONS["Can't separate"], reason)
    if day > max(values):
        if blocked:
            reason = f"The next change, on {after}, came before {AFTER} full weeks had passed."
            return verdict("Can't separate", 0, ACTIONS["Can't separate"], reason)
        action = f"Re-check on or after {recheck}."
        return verdict("Too early", 0, action, "There is no data after this change yet.")
    before, later = weeks_before(values, day, since), weeks_after(values, day, after)
    if len(before) < MIN_WEEKS:
        cut = since and day - timedelta(days=7 * (len(before) + 1)) < since["start"]
        where = f", counting from {since['start']}, {since['why']}" if cut else ""
        action = f"Add older data, or wait until there are {MIN_WEEKS} full weeks before a change."
        reason = f"Only {len(before)} of {MIN_WEEKS} full weeks before it{where}."
        return verdict("Can't judge", len(later), action, reason)
    chart = Chart([level for _, level in before], nonnegative)
    if not chart.varies:
        reason = "The weeks before it show no week-to-week variation, so there is no normal range."
        return verdict(
            "Can't judge", len(later), "Check that the metric is being recorded.", reason
        )
    rows = [marked(chart, x, start, level, "before") for x, (start, level) in enumerate(before)]
    if unsteady := first_signal(rows):
        action = (
            f"Log what happened in the week of {unsteady} as a change dated the day it "
            "started, then run it again."
        )
        reason = (
            f"The weeks before it were already unsteady (week of {unsteady}), so there is no "
            "stable normal to compare with."
        )
        return verdict("Can't judge", len(later), action, reason)
    rows += [
        marked(chart, chart.n + x, start, level, "after") for x, (start, level) in enumerate(later)
    ]
    return outcome(chart, before, rows, after, blocked, recheck)


def weeks_before(values, day, since):
    weeks = []
    for back in range(1, MAX_WEEKS + 1):
        start = day - timedelta(days=7 * back)
        level = week_level(values, start)
        if level is None or (since and start < since["start"]):
            break
        weeks.append((start, level))
    return weeks[::-1]


def weeks_after(values, day, after):
    weeks = []
    for ahead in range(AFTER):
        start = day + timedelta(days=7 * ahead)
        if after and start + timedelta(days=6) >= after:
            break
        level = week_level(values, start)
        if level is None:
            break
        weeks.append((start, level))
    return weeks


def outcome(chart, before, rows, after, blocked, recheck):
    later = rows[chart.n :]
    above = [row for row in later if row["side"] == "above"]
    below = [row for row in later if row["side"] == "below"]
    upper = sum(row["half"] == "upper" for row in later)
    lower = sum(row["half"] == "lower" for row in later)
    up, down = len(above) >= 2 or upper >= 3, len(below) >= 2 or lower >= 3
    detail, weeks = {"chart": chart, "before": before, "rows": rows}, len(later)
    if up != down:
        label, outside, near = ("Moved up", above, upper) if up else ("Moved down", below, lower)
        side, end = ("above", "top") if up else ("below", "bottom")
        if len(outside) >= 2:
            reason = f"{len(outside)} of the {weeks} weeks after it were {side} the expected range."
        else:
            reason = (
                f"{near} of the {weeks} weeks after it were in the {end} quarter of the expected "
                "range or beyond."
            )
        return verdict(label, weeks, ACTIONS[label], reason, **detail)
    if weeks == AFTER:
        if bool(above) != bool(below):
            label, special = ("One-off spike", above) if above else ("One-off dip", below)
            special = special[0]["start"]
            reason = (
                f"Only the week of {special} was {'above' if above else 'below'} the expected "
                "range; the other weeks were back inside it."
            )
            return verdict(label, weeks, ACTIONS[label], reason, special=special, **detail)
        if above:
            reason = (
                "Weeks after it went both above and below the expected range, which is not a "
                "lasting move either way."
            )
        else:
            reason = (
                f"The {AFTER} weeks after it stayed within routine variation. It would likely "
                f"have caught a lasting move of {chart.smallest_move()}."
            )
        label = "No detectable change"
        return verdict(label, weeks, ACTIONS[label], reason, swings=bool(above), **detail)
    so_far = "".join(
        f" The week of {row['start']} was {row['side']} the range so far."
        for row in above[:1] + below[:1]
    )
    if blocked:
        reason = f"The next change, on {after}, came before {AFTER} full weeks had passed."
        return verdict(
            "Can't separate", weeks, ACTIONS["Can't separate"], reason + so_far, **detail
        )
    reason = f"{weeks} of the {AFTER} weeks after it are complete.{so_far}"
    return verdict("Too early", weeks, f"Re-check on or after {recheck}.", reason, **detail)


def verdict(label, weeks, action, reason, **detail):
    return {"label": label, "weeks": weeks, "action": action, "reason": reason, **detail}


def marked(chart, x, start, level, when):
    low, low_half, _, high_half, high = chart.bounds(x)
    value = chart.to_chart(level)
    side = "above" if value > high else "below" if value < low else "inside"
    half = "upper" if value > high_half else "lower" if value < low_half else ""
    return {
        "start": start,
        "level": level,
        "range": chart.span_text(x),
        "when": when,
        "side": side,
        "half": half,
    }


def first_signal(rows):
    """Rule 1 (a week beyond a limit) or rule 3 (three of four beyond one midpoint)."""
    for row in rows:
        if row["side"] != "inside":
            return row["start"]
    for index in range(len(rows) - 3):
        window = rows[index : index + 4]
        for half in ("upper", "lower"):
            if sum(row["half"] == half for row in window) >= 3:
                return window[0]["start"]
    return None


# The report


def report(title, values, changes, notes):
    first, last = min(values), max(values)
    verdicts = judge_all(values, changes)
    lines = [
        title,
        "",
        f"Daily values from {first} to {last}, grouped into 7-day weeks that start on each "
        "change date.",
        *notes,
        "",
        "| Change date | Verdict | Weeks after | What to do | What changed |",
        "|---|---|---|---|---|",
    ]
    for day, what, result in verdicts:
        lines.append(
            f"| {day} | {result['label']} | {result['weeks']} | {result['action']} "
            f"| {cell(clip(what))} |"
        )
    for day, what, result in verdicts:
        lines += ["", f"## {day}: {cell(what)}", "", f"**{result['label']}.** {result['reason']}"]
        if chart := result.get("chart"):
            lines += ["", chart.describe(result["before"]), ""]
            lines += ["| Week starting | Average per day | Expected range | |", "|---|---|---|---|"]
            for row in result["rows"]:
                where = row["when"] if row["when"] == "before" else f"after, {row['side']}"
                level = fmt(row["level"], chart.decimals)
                lines.append(f"| {row['start']} | {level} | {row['range']} | {where} |")
    lines += [
        "",
        "## How this was judged",
        "",
        "- Each week's value is its average per day, in 7-day weeks that start on the change date.",
        f"- The {MIN_WEEKS}-{MAX_WEEKS} full weeks before a change set the expected range: the "
        f"average plus or minus {SCALE} times the average week-to-week change (Wheeler's XmR "
        "process behaviour chart). If those weeks were already on a clear trend, the range "
        "follows the trend and widens the further it projects.",
        f"- A lasting move needs, within the first {AFTER} weeks after the change, two weeks "
        "outside the range on the same side, or three in the outer quarter on the same side.",
        "- Changes are judged in date order. Only a change with no detectable effect lets its "
        "weeks count toward a later change's normal range.",
        "- In simulation, an unchanged metric still shows a lasting move about 5% of the time, "
        "and a lasting move as large as the edge of the range is caught about 86% of the time.",
        "- This shows timing, not cause. Check those weeks for outside events: holidays, press, "
        "outages, other launches.",
        "",
        "Keep a change log to run this again: one line per change, `YYYY-MM-DD | what changed`.",
    ]
    return lines
