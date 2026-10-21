# Connection requests

`social.linkedin_requests` (#450) sends the LinkedIn connection requests a member approved, one
at a time, from their own saved session, then reads who accepted. It follows
[People who engaged with your LinkedIn posts](linkedin-engagers.md), whose people it proposes.
Members can also choose people themselves in People. Boards: Paper "Warm reach · connection
requests", INVITE-A to INVITE-C and REQ-A.

## Proposed

At the end of an engagers run, `linkedin_requests.propose` puts everyone newly eligible into
the starter's open batch (one open batch per member and project). Someone is eligible when:

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
them. A run with none left only reads who accepted and would sleep through new ones, so a new
run starts, with the start key `linkedin-requests:<batch>`. Joining, starting and a run's
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

The run asks `linkedin_requests_next` what to do and follows: send, wait, read who accepted,
or finish. It sends every batch it holds. Every decision about time is made from Postgres, so history holds only request IDs,
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

## Who accepted

About a day after the newest request, and then daily, `linkedin_requests_check` reads the
relationship of up to 20 waiting people (`linkedin_relationships`): connected becomes
`accepted` (with an `accepted` event), still invited stays, and neither becomes `not_pending`
(declined or withdrawn) or, for an uncertain one, `not_sent`. Requests stop being read three
weeks after they were sent. Withdrawing old requests isn't built yet: no LinkedIn endpoint for
it has been verified.

When nothing is queued or waiting, the run writes `people/connection-requests.md` (who, and
what happened, for every batch it held) and finishes with a one-line summary. A run that fails
lapses what it had queued, from every batch it held; those people can be chosen again.

## Where it shows

- **Decisions:** the batch's card lists each person with their headline, ICP, fit and what they
  engaged with; unticking skips someone. It says when the first and last would go.
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
