# Connection requests

`social.linkedin_requests` (#450) sends the LinkedIn connection requests a member approved, one
at a time, from their own saved session, and finishes once the last one is sent. The member's
LinkedIn reads then read who accepted. It follows
[People who engaged with your LinkedIn posts](linkedin-engagers.md) and
[People who viewed your LinkedIn profile](linkedin-profile-viewers.md), whose people they propose.
Members can also choose people themselves in People. Boards: Paper "Warm reach · connection
requests", INVITE-A to INVITE-C and REQ-A.

## Proposed

At the end of an engagers or profile viewers run, `linkedin_requests.propose` puts everyone
newly eligible into the starter's open batch (one open batch per member and project). Someone is
eligible when:

- they reacted to or commented on the starter's own posts, or viewed the starter's profile
  ([profile viewers](linkedin-profile-viewers.md)), as events from runs the starter began;
- their ICP fit is 0.6 or more, whoever set it;
- the starter isn't connected to them or already inviting them (their own row in
  `record_relationships` is `none`);
- they aren't marked a customer;
- the starter never had them in a batch before, decided or not.

The batch waits in Decisions under "Connection requests", for that member only: another
member's LinkedIn would send it, so nobody else sees or decides it. Over MCP,
`list_open_decisions` returns it as `connection_requests`.

## Approved

Approving (`POST /api/projects/{project_id}/linkedin-requests/batches/{id}/approve`, or
`approve_connection_requests`) queues the people the member ticked and declines the rest;
discarding declines everyone. Nobody declined is proposed again. The same `request_id`
repeats the same decision. Approving needs sending to be on, and hands the batch to the
member's sending run (see [One line per member](#one-line-per-member)); nothing else can start
one. Nothing is sent at the moment of approval. If a new run can't start, the batch goes back
to Decisions as it was and the member reads why.

## Approved automatically

A member can let Tin approve for them: "Approve people who fit automatically" on the LinkedIn
card's Sending row (`PUT …/linkedin-requests/sending` with `auto_approve: true`, which takes
`enabled: true` and `consent_version` 1 like any change to sending). It needs sending on, and
turning sending off turns it off too, so turning sending on again later doesn't bring it back
unasked. MCP can't turn it on, as it can't turn sending on.

Then, right after each of the member's engagers or profile viewers runs proposes, Tin approves
their open batch for them (`ConnectionRequests.auto_approve`):

- **Who:** people still proposed whose fit is still 0.6 or more, who aren't a customer and
  aren't the member's connection or invitee as far as Tin knows now. The rest of the batch is
  declined, as unticked people are.
- **How:** through the same `decide` as Decisions, as the member, so the member-only rule, the
  caps, the window, the wait between requests, the halved first week, the 14-day lapse and
  pauses all apply as usual. It runs after `propose` commits, because approving starts or joins
  the member's sending run.
- **When not:** sending off, or paused until the member resumes it; the batch then waits in
  Decisions as usual. A refusal (a run that can't start) leaves the batch there too.
- **Retries:** the decision's `request_id` comes from the run that proposed
  (`uuid5(AUTO_APPROVE, "auto-approve:<run>")`), so a retried read repeats the same approval
  and starts no second run.
- **How it shows:** the run's line ("Approved automatically: connection requests to 2 people
  who fit. Tin sends them one at a time on your schedule."), and a sending run it starts reads
  "approved automatically" in Activity (`trigger_source` `auto_approve`).

## Chosen in People

A member can tick people in People and press "Send N requests"
(`POST /api/projects/{project_id}/linkedin-requests/send`, or `send_connection_requests`).
Choosing is the approval: `linkedin_requests.pick` queues them, in the order sent, in a batch
of their own that is approved at once, and hands it to the member's sending run like an
approved batch. Anyone chosen must be someone the member can ask (`pickable`): with a LinkedIn
profile ID, not a customer, not the member's connection or invitee as far as Tin knows, and
with no request from the member that went out or waits. A request that lapsed, was declined in
Decisions or never went out doesn't stop them; someone waiting in the member's batch in
Decisions moves to the new one. The fit doesn't matter: the member chose them. Up to 100 at a
time; the same `request_id` repeats the same choice. If a new run can't start, the batch is
removed, anyone it took from Decisions waits there again, and the member reads why.

## One line per member

A member's requests on a project wait in one line, whichever way they were approved, and one
`social.linkedin_requests` run sends them in the order they were queued. Each member has their
own line, so two members can each ask the same person.

`linkedin_requests.sending_run` decides which run sends an approved batch. A batch joins the
member's run on the project (`send_run_id`) while that run still has requests waiting, behind
them. A run with none left is finishing, so a new run starts, with the start key
`linkedin-requests:<batch>`. A member has at most one sending run on a project. Joining, starting and a run's
decision to finish all take the member's line lock, and a new run is recorded on its batch
before the lock is released, so two approvals at once share one run and no batch joins a run
that is finishing. A run that reaches its step limit carries on as the same run (continue as
new) rather than finish with requests waiting.

Before it goes, the member can cancel a request of theirs from the person's panel ("Cancel
request", `POST …/linkedin-requests/people/{person_id}/cancel`, or
`cancel_connection_request`). It reads as declined, so they can be chosen again; one being
sent right now can't be cancelled. Marking someone a customer declines every member's request
to them that still waits, and the sender checks again just before each send. The panel's
LinkedIn row says when a queued request would go (`linkedin_requests.queued`).

People and `list_people` say who the viewer can ask (`requestable`, `can_request`) and whose
request of theirs waits to go out (`request`, `request_queued`). The requests state carries
`next_open`, when one more request would go, for the bar above the grid.

## Sent, one at a time

Sending is per member and project. It is on by default for a member whose LinkedIn reading
runs in the cloud, whose saved session it sends with; reading in this browser only keeps it
off. Members agree to sending when they join Tin, so a member who never set it reads as on,
and their first approval (in Decisions, People or over MCP) records it with `consent_version` 1,
so a pause has a setting to hold. A member who turns it off on the LinkedIn card stays off until
they turn it on again there (`PUT …/linkedin-requests/sending` with `consent_version` 1; the
consent box starts ticked). MCP can't turn it back on. The card says plainly that LinkedIn's terms don't allow automated requests
and that pacing lowers the risk to the account without removing it.

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

Approvals not sent within 14 days lapse.

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

- **Decisions:** the batch's card lists each person with their headline, ICP, fit and what they
  did ("reacted", "viewed"); unticking skips someone. It says when the first and last would go.
  Its title names what brought them, in the words of the workflow that found them: "People who
  engaged with your LinkedIn posts", "People who viewed your LinkedIn profile", or "People who
  engaged with your posts or viewed your profile" for a batch with both.
- **LinkedIn card:** the Sending row (switch, consent, approving automatically, limits, the
  paused state with Resume) and This week (sent, accepted, queued, pending, next send). A held sending makes the card need
  you.
- **People:** the tick lane and its bar; "Queued", then "Invited oct 16", in the LinkedIn
  column; in the panel, when a queued request goes and "Cancel request" (Paper REQ-C2); an Invited segment once someone is invited; and "Connection request sent" /
  "Accepted your connection request" on the timeline ("A teammate sent a connection request"
  for someone else's).

The sandbox image needs the new runner kinds: build a new `tin-linkedin-*` alias from
`sandbox/linkedin/Dockerfile` and point `TIN_LITE_LINKEDIN_TEMPLATE` at it, staging first.
Deleting a project removes its sending settings, batches, requests and relationships.
