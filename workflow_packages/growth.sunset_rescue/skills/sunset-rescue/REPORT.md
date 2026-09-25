# Report structure

Write the report in this order. Keep headings exactly as written so later runs can read it.

```markdown
# Sunset rescue: <YYYY-MM-DD>

Status: rescue | watch_only | insufficient_context
Chosen event: <tool> <event type>, deadline <YYYY-MM-DD> (<phase>), or "none this week"

## The short version
Three to five sentences for the founder: what happened, whose users are moving, why this
product is or is not a good place for them, and the single next action with its date.

## The job and import paths
The job sentence, the three core tasks, and the import paths found, each with its cited source.

## Events found
| Tool | Ring | Event type | Announced | Deadline | Export ends | Phase | Primary source |

## Scores
| Tool | Displacement | Fit | Portability | Reachability | Phase weight | Score | Veto |
One line under the table per event explaining the Fit and Portability numbers.

## Rescue kit: <tool>
Leave this section out when the status is not rescue.

### Export anatomy
How users export (steps, format, deadline) with citations, then a table:
| Their field | Lands in this product as | Carries over / with loss / does not |

### Import gap
The existing import path, the manual steps, or a proposed importer spec marked "proposal".

### Landing page draft
Title, a short target-search line, then the full draft text in plain Markdown.

### Where the refugees are asking
| Place | Link | Date | What they keep asking | Community rules on promotion |
A drafted reply for each place, with the affiliation disclosed.

### Deadline clock
| Date | Phase | Action | Owner (founder or Tin workflow) |

### How we will know it worked
Three signals, where each is read, and whether the project can measure it today.

## Watch list
| Tool | Signal | Status (confirmed, unconfirmed, fragility) | Reconsider on | Source |

## Evidence and assumptions
Mark every inference as an inference. List searches that returned nothing useful, so the
next run does not repeat them blindly.
```

Status meanings:

- rescue: one event cleared every veto and a full kit is included.
- watch_only: events were checked and none cleared, or none were found. Include the watch
  list.
- insufficient_context: Station 1 could not define the job from evidence. Say what to add
  to the project.
