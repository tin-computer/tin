# Evidence format for voice-mining

The evidence line is the citation format for every quote in the brief. The fenced Python
block defines `EvidenceLine.fmt`, `next_evidence` and `fence`; extract it into a scratch
module and use it unchanged.

## Evidence line

Every quote in the brief carries one line under it:

```text
> — {sender}, "{subject}" (gmail · {search_index} · read {utc_time})
```

- `sender`: the display name, or the email address when no name is shown.
- `subject`: the message subject, never empty; use `(no subject)` when absent.
- `search_index`: which of this run's searches surfaced it, 1-based.
- `utc_time`: the run's UTC time, `YYYY-MM-DD HH:MM`.

One line, no extras. In the evidence block's quotes, `sender` and `subject` stay identical
so a reader can jump from the report to the inbox record.

## Python block

```python
import json

VERSION = "voice-mining-evidence-v1"


class EvidenceLine:
    def __init__(self, sender, subject, search_index, utc_time):
        self.sender, self.subject = sender, subject
        self.search_index, self.utc_time = search_index, utc_time

    def fmt(self):
        return '> — {0}, "{1}" (gmail · {2} · read {3})'.format(
            self.sender, self.subject or "(no subject)", self.search_index, self.utc_time
        )


def next_evidence(quotes, checked_at):
    """quotes: list of dicts {sender, subject, search_index, theme, quote}."""
    return {
        "version": VERSION,
        "checked_at": checked_at,
        "quotes": quotes,
    }


def latest(text):
    """Read the newest valid block from an earlier report, or None."""
    start = text.rfind('```json')
    if start == -1:
        return None
    start = start + len('```json')
    end = text.find('```', start)
    if end == -1:
        return None
    try:
        payload = json.loads(text[start:end])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != VERSION:
        return None
    if not isinstance(payload.get("quotes"), list):
        return None
    return payload


def fence(payload):
    return "```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```"
```

`latest` returns the payload only when its version matches; an earlier or malformed block is
carried as "unreadable evidence block" under Not read, never diffed against.

## Report layout

The brief has these sections in order. `context.output.path` receives exactly this file.

```markdown
# Voice mining — {date, YYYY-MM-DD}

## Sources

{Which inputs and files supplied the product words, and which searches ran, numbered in
order, with the query string of each. Keyword: the pool of at most six product words.}

## Quotes

{3-10 quotes, each the exact phrase, one evidence line under it, grouped under the theme
headers Outcome, Objection, Switch. A quote appears once.}

## Not read

{Searches not run because the budget ran out (`error`, "budget"); senders that are
teammates or no-reply addresses; messages that were receipts or templates; searches that
returned nothing usable (`empty`). Each on one line with its reason.}

## Assets

### Outreach one-liners

{Three lines, each: the question, then the quote it rests on, then the evidence line.}

### Referral ask

{2-4 sentences addressed to one sender, followed by the evidence line of the quote it
rests on. Never a sent message; the founder copies it.}

### Web copy

{Two entries: where it belongs, the quote, the compressed line, and [assumption] markers
on any claim no quote supports.}

## Evidence block

{fence(next_evidence(...)) — the block the next run reads back}
```
