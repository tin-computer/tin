# Report structure

Write the report in this order. Keep headings exactly as written so later runs can read it.

````markdown
# Sunset rescue: <YYYY-MM-DD>

Status: rescue | watch_only | insufficient_context
Chosen event: <tool> <event type>, deadline <YYYY-MM-DD> (<phase>), or "none this week"

## The short version
Three to five sentences for the founder: what happened, whose users are moving, why this
product is or is not a good place for them, and the single next action with its date.

## The job and import paths
The job sentence, the three core tasks and the import paths found, each with its cited source.

## Events found
| Tool | Ring | Event type | Announced | Deadline | Export ends | Phase | Primary source |

## Scores
| Tool | Displacement | Fit | Portability | Reachability | Phase weight | Score | Veto |
One line under the table per event explaining the Fit and Portability numbers, and the demand
evidence behind Displacement (keyword plan run id and volume, or "demand unmeasured").

## Migration kit: <tool>
Leave this section out when the status is not rescue.

### Export anatomy
How users export (steps, format, deadline) with citations, then a table:
| Their field | Lands in this product as | Carries over / with loss / does not |

### Import gap
The existing import path as numbered steps, or a proposed importer spec marked "proposal".

### Hand-offs
- Draft a public article: the `brief`, in a fenced text block.
- One-off project task: the `title` and `instruction`, or "no import gap".
- Find threads where people ask for what you make: the `focus`.

### Deadline clock
| Date | Phase | Action | Tin workflow or founder |

### How we will know it worked
Three signals, where each is read, and whether the project can measure it today.

## Watch list
| Tool | Signal | Status (confirmed, unconfirmed, fragility) | Reconsider on | Source |

## Evidence and assumptions
Mark every inference as an inference. List the searches run and those that returned nothing.

```json tin-sunset-rescue
{
  "version": 1,
  "run_date": "YYYY-MM-DD",
  "status": "rescue | watch_only | insufficient_context",
  "chosen": {"tool": "...", "event_type": "...", "deadline": "YYYY-MM-DD", "phase": "...", "source": "https://..."},
  "kits_delivered": [{"tool": "...", "event_type": "...", "phase": "...", "run_date": "YYYY-MM-DD"}],
  "watch_list": [{"tool": "...", "signal": "...", "status": "confirmed | unconfirmed | fragility", "reconsider_on": "YYYY-MM-DD", "source": "https://..."}],
  "searches": [{"query": "...", "useful": false}]
}
```
````

`chosen` is `null` unless the status is rescue. `kits_delivered` carries forward every kit from
earlier evidence blocks plus this run's. The block is the last thing in the report.

Status meanings:

- rescue: one event cleared every veto and a full migration kit is included.
- watch_only: events were checked and none cleared, or none were found. Include the watch
  list.
- insufficient_context: Station 1 could not define the job from the project's files. Say which
  workflow would supply what is missing.
