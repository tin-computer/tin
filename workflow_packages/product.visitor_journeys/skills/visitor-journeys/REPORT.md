# Report

One Markdown file, in this order. Plain sentences, no marketing words, no emoji. Times are UTC.
Leave out a section with nothing in it, except Status and Numbers.

```text
# Visitors, <start> to <end> UTC

Status: <complete | incomplete | nothing to report | invalid configuration>

<One sentence: the most important thing today, for example "Two hackathon signups stalled on
Connect GitHub; the pricing calculator failed four times for one visitor.">

## People to act on today
### <Name or email> · <lead | new user | user>
- **Who:** <email>, <what their own data says about them>.
- **Journey:** <3 to 6 steps, ending where they stopped>.
- **Stopped at:** <page or step, and the likely reason>.
- **Suggested:** <one action, with a short draft if it is a message>.
- [Watch the replay](<replay link>)

## New users and activation
<Each new user in one line: where they came from, how far they got against the map's steps.>

## Frictions
### <What happens, in words> · <new | ongoing | fixed>
- **Where:** <page and element, or event>.
- **Who:** <names of identified people; a count of anonymous ones>.
- **Cause:** <likely cause, the file and line if found, or "cause unknown">.

### Smaller frictions
- <What happens> on <page>: <who>.

## Delights
- <Person, the moment, and why it matters.>

## Product changes
1. **<Change>** — <evidence>, <cause>, <file if known>, <the smallest fix>.

## Tracking
- <Problem>: <exact fix>.

## Numbers
| | People |
|---|---|
| Everyone in the window | <n> |
| Identified | <n> |
| Leads / new users / users | <n> / <n> / <n> |
| Anonymous browsers / passers-by | <n> / <n> |
| Team / bots left out | <n> / <n> |

Sources: <top referrers and campaigns>. Countries: <top five>.

## How this was measured
<The window, the map's changes this run, each query's step with rows and truncation, and
anything missing.>
```

The report ends with the evidence block from `render_evidence()`, exactly once, after
everything else. Tin reads it: the Status line must match it, and every named person in it must
appear in the report above. Tin writes each named person to the project's People with their
summary, tier and replay link, so write the summary for someone who reads only that line.
