# Connection requests

`social.linkedin_requests` (#450) sends a member's LinkedIn connection requests, one at a time,
from their own saved session, and finishes once the last one is sent. The member's LinkedIn
reads then read who accepted. It follows
[People who engaged with your LinkedIn posts](linkedin-engagers.md) and
[People who viewed your LinkedIn profile](linkedin-profile-viewers.md), which queue the people
they find who fit. Members can also choose people themselves in People. Nothing waits in
Decisions: while sending is on, Tin asks. Boards: Paper "Warm reach · connection requests",
INVITE-A to INVITE-C and REQ-A.

## Queued after each read

At the end of an engagers or profile viewers run, `linkedin_requests.queue` queues a request
from the starter to everyone who:

- reacted to or commented on the starter's own posts, or viewed the starter's profile
  ([profile viewers](linkedin-profile-viewers.md)), as events from runs the starter began;
- has an ICP fit of 0.6 or more, whoever set it;
- isn't connected to the starter or already invited (the starter's own row in
  `record_relationships` is `none`);
- the starter can ask (`pickable`, below) and never cancelled a request to.

Best fit first, in a batch of their own that is approved at once (`source_run_id` is the read),
handed to the member's sending run (see [One line per member](#one-line-per-member)). The same
read repeats the same batch. While sending is off, nothing is queued and the people simply wait;
the next read after sending is back on asks them. If the sending run can't start, the batch is
removed and the read's line says why; the next read tries again. The read's line says how many
it queued ("2 connection requests queued for people who fit.").

Members used to approve these in Decisions. Anyone still proposed in an old open batch is
queued by the member's next read, from the same row.

## Chosen in People

A member can tick people in People and press "Send N requests"
(`POST /api/projects/{project_id}/linkedin-requests/send`, or `send_connection_requests`).
`linkedin_requests.pick` queues them, in the order sent, in a batch of their own that is
approved at once, and hands it to the member's sending run. Anyone chosen must be someone the
member can ask (`pickable`): with a LinkedIn profile ID, not a customer, not the member's
connection or invitee as far as Tin knows, and with no request from anyone on the project that
went out or waits. A request that lapsed, was declined or never went out doesn't stop them. The
fit doesn't matter: the member chose them. Up to 1,000 at a time; the same `request_id` repeats
the same choice. If a new run can't start, the batch is removed and the member reads why.

## One line per member

A member's requests on a project wait in one line, whichever way they were queued, and one
`social.linkedin_requests` run sends them in the order they were queued. Each member has their
own line, but nobody on a project hears from two of its members: once someone on the project
asked a person, or queued a request to them, nobody else on it can. Queueing and choosing take
the project's lock (`linkedin-asks:<project>`), so two members can't both take the same person.

`linkedin_requests.sending_run` decides which run sends a batch. A batch joins the
member's run on the project (`send_run_id`) while that run still has requests waiting, behind
them. A run with none left is finishing, so a new run starts, with the start key
`linkedin-requests:<batch>`. A member has at most one sending run on a project. Joining, starting and a run's
decision to finish all take the member's line lock, and a new run is recorded on its batch
before the lock is released, so two batches at once share one run and no batch joins a run
that is finishing. A run that reaches its step limit carries on as the same run (continue as
new) rather than finish with requests waiting.

Before it goes, the member can cancel a request of theirs from the person's panel ("Cancel
request", `POST …/linkedin-requests/people/{person_id}/cancel`, or
`cancel_connection_request`). It reads as declined: a read doesn't ask them again, though the
member can still choose them in People. One being sent right now can't be cancelled. Marking someone a customer declines every member's request
to them that still waits, and the sender checks again just before each send. The panel's
LinkedIn row says when a queued request would go (`linkedin_requests.queued`).

People and `list_people` say who the viewer can ask (`requestable`, `can_request`) and whose
request of theirs waits to go out (`request`, `request_queued`). The requests state carries
`next_open`, when one more request would go, for the bar above the grid.

## Sent, one at a time

Sending is per member and project. It is on by default for a member whose LinkedIn reading
runs in the cloud, whose saved session it sends with; reading in this browser only keeps it
off. Members agree to sending when they join Tin, so a member who never set it reads as on,
and their first requests (queued by a read, or chosen in People or over MCP) record it with
`consent_version` 1, so a pause has a setting to hold. A member who turns it off on the LinkedIn
card stays off until they turn it on again there (`PUT …/linkedin-requests/sending` with
`consent_version` 1; the consent box starts ticked). MCP can't turn it back on. The consent
reads: "Send connection requests from my LinkedIn to people who fit my ICP and engaged with my
posts or viewed my profile, and to anyone I choose. A few each weekday, stopping at LinkedIn's
first warning. I can turn it off anytime."

The run asks `linkedin_requests_next` what to do and follows: send, wait, or finish once
nothing is left to send. It sends every batch it holds. Every decision about time is made from Postgres, so history holds only request IDs,
waits and outcomes. A request may go when, for the member across all their projects:

- it's a weekday inside the window (09:00–17:00 by default) in the founder's time zone: the one
  Start here recorded, else the newest saved schedule's, as the organic system reads it;
- at least 15 minutes have passed since their last request, plus a wait of up to 25 minutes
  that each request draws once from its ID;
- they're under the daily and weekly caps (15 and 60 by default, half in their first week of
  sending; the member can change them within 1–25 and 1–100);
- sending isn't paused.

`linkedin_requests_send` reserves the turn under a per-member lock, then sends in the LinkedIn
sandbox under an effect receipt (`linkedin_request_v1`). The sandbox's `linkedin_invite` kind
checks `/me`, reads the person's top card, and posts the request (no note) only when the card
shows neither a connection nor an invitation. Outcomes:

| Outcome | Request | Sending |
|---|---|---|
| sent | `sent`; the person reads "Invited", with an `invited` event | goes on |
| already connected or invited | `connected` / `invited`, nothing sent | goes on |
| LinkedIn didn't show the relationship | `unknown_person`, nothing sent | goes on |
| refused by LinkedIn | `refused` | paused until the member resumes |
| LinkedIn's weekly limit | queued again | held until Monday's window |
| a checkpoint, signed out, blocked, slow down | queued again | paused until the member resumes |
| may have gone out (no answer, a sandbox that started and stopped) | `uncertain`, never sent again | goes on |

A queued request waits in the member's line, however long, until it is sent, the member
cancels it, or the person can't be asked any more: connected or invited (the fresh read before
sending), marked a customer, or forgotten. The sending limits set the pace, so a long list is
queued once and worked through. While sending is off or paused, the run waits with it.

When nothing is queued, the run writes `people/connection-requests.md` (who, and what happened,
for every batch it held) and finishes with a one-line summary ("13 connection requests sent.").
A run that fails lapses what it had queued, from every batch it held; those people can be
chosen again.

## Who accepted

Not the sending run: it would sit in Running for weeks only to wait. The member's own LinkedIn
reads read it instead, after their main reads and before they publish:
[profile viewers](linkedin-profile-viewers.md) (daily) and [engagers](linkedin-engagers.md).

- **When:** about once a day (`linkedin_requests.reading_due`): while a request of the member's
  on the project waits for an answer, and the last read was 20 hours ago or more, whichever of
  their runs comes first. Not after LinkedIn objected earlier in the run.
- **What:** one sandbox read (`linkedin_relationships`) of up to 20 waiting people, oldest
  first, under the run's effect receipt (`<run>:<engagers|viewers>:accepted`):
  - connected becomes `accepted`, with an `accepted` event;
  - still invited stays;
  - neither becomes `not_pending` (declined or withdrawn) or, for an uncertain one, `not_sent`.
- **How it shows:** People and the LinkedIn card, and the run's line ("1 person accepted your
  connection request.").
- **Problems:** a read that can't run counts as done for the day. LinkedIn objecting pauses the
  member's sending, as a send would.
- **Nobody answers:** a request stops being read three weeks after it was sent. The person
  stays "Invited" in People and the request stays pending on LinkedIn. Withdrawing old requests
  isn't built yet: no LinkedIn endpoint for it has been verified.

A member with neither workflow running isn't read. Sending runs from before this change still
read who accepted themselves when they ask to (`linkedin_requests_check`), from the same
helper, `read_accepted`.

## Where it shows

- **LinkedIn card:** the Sending row (switch, consent, limits, the paused state with Resume) and
  This week (sent, accepted, queued, pending, next send). A held sending makes the card need
  you.
- **People:** the tick lane and its bar; "Queued", then "Invited oct 16", in the LinkedIn
  column; in the panel, when a queued request goes and "Cancel request" (Paper REQ-C2); an Invited segment once someone is invited; and "Connection request sent" /
  "Accepted your connection request" on the timeline ("A teammate sent a connection request"
  for someone else's).

The sandbox image needs the new runner kinds: build a new `tin-linkedin-*` alias from
`sandbox/linkedin/Dockerfile` and point `TIN_LITE_LINKEDIN_TEMPLATE` at it, staging first.
Deleting a project removes its sending settings, batches, requests and relationships.
