# People records

A project's people records hold one lasting record per person the project knows. This is the
first slice of the records design (#435): Tin's own workflows write them, and members read them
in [the People view](#the-people-view). Custom fields, saved segments, a commit log and agent
copies (`pull_records`, `commit_record_changes`) come later.
`src/tin_lite/people_records.py` is the only writer.

## Tables

| Table | Holds |
|---|---|
| `records` | One row per person, with a `type` (only `person` today) and where it first came from |
| `record_identifiers` | How a record is recognized: a LinkedIn member ID or profile slug, or an email, unique per project |
| `record_fields` | Field definitions: built-in today, custom later without a migration |
| `record_values` | One current value per record and field, with its lineage |
| `record_events` | The timeline: `reacted`, `commented`, `invited`, `accepted`, `withdrawn`, `visited` |
| `record_suppressions` | A hash of each identifier of a person the founder asked Tin to forget |

## Recognizing a person

`email` is an address the person gave the project's product, read by
[visitor journeys](visitor-journeys.md) from their PostHog person. It is compared without case
and checked for shape. Anonymous visitors never get a record.

`linkedin_identifiers` reads a profile URL or URN:
- `/in/ACo…` and `urn:li:fsd_profile:ACo…` are LinkedIn's own profile ID (`linkedin_member`);
- any other `/in/<slug>` is the public slug (`linkedin_slug`), compared without case.

`upsert_person` returns the record that any of the given identifiers already names, or makes
one. It takes a per-project lock, so two reads of the same person never create two records.

When one read names two records (a slug on one, a member ID on the other), they merge:
- the older record survives, and the newer points at it with `merged_into`;
- identifiers move to the survivor;
- the survivor keeps its own values, and a field only the newer record had moves over;
- events stay where they were written, and reads count them under the survivor.

## Values

Each value records:
- who set it: a run's ID or a member's user ID (`set_by_kind` is `run` or `member`);
- when, and a one-line reason;
- the definition it was computed under, such as the ICP file's revision, so a changed
  definition can show old values as stale.

An empty value can still carry a reason, such as an ICP score the model couldn't support.
A run never replaces a value a member set. A write that changes nothing keeps the earlier
value and its time, unless the writer asks for a new time: the engagers run does when it scores
someone again after a note, so the score counts as newer than the note even when unchanged.

A member's value keeps the run's value it replaced in `previous`. `restore_run_values` puts
that value back ("Use Tin's score"); a member's value with no run value behind it is removed, so
the next run fills the field.

The built-in fields are:
- `name`, `headline` (text);
- `icp` (which named profile in the ICP file, or `none`);
- `icp_fit` (a number from 0 to 1);
- `customer` (`yes` or `no`), which only members set;
- `web_tier` (`lead`, `new_user` or `user`: what they were on the site when visitor journeys last
  read them, with where they stopped as the reason) and `web_summary` (that run's one-line
  summary). A visitor journeys run sets `name` only when no other source has.

## LinkedIn relationships

Whether someone is connected is each member's own: a person can be one teammate's connection
and a stranger to another. `record_relationships` holds one row per person and member
(`connected`, `pending` or `none`), with the run that read or changed it. An engagers run
writes its starter's, from each reaction's degree; a connection request writes its sender's.
A merge keeps the survivor's row for each member, else the newest. The `linkedin_relationship`
field from before is no longer written or read; migration 071 moved each run's value to the
member who started the run.

## Notes

`record_notes` holds what members write about a person: up to 2,000 characters each, with the
author and when. Only the author edits or deletes a note. A merge moves notes to the survivor.

The engagers run sends a person's five newest notes with their name and headline when it scores
them, and the instructions weigh a note above the headline. A note newer than a run's score
makes that score stale: the view marks it, and `unscored` picks the person up again. A fit a
member set is never sent to the model.

## Events

Events are only added. Each has an `event_key` that is unique per project: an engagement
keys on the post, the person and the kind, so reading the same post again adds nothing. A
`visited` event keys on the person and the run's window end date, so a retried or overlapping
run adds one a day; it holds the summary, where they stopped, the suggested action and a
PostHog replay link. An event written to a record that later merged counts under the survivor.

## The People view

`/people` lists a project's people for its members (`src/tin_lite/static/people.js`, Paper
boards PEOPLE-A1, C1, C1b, E1–E3, N1 and N1b). It reads `GET /api/projects/{project_id}/people`
(`people_records.directory`), at most 5,000 people, newest first, with the ICP file's profiles;
the page sorts, searches, segments and pages them. The HTTP routes (`people_api.py`) and the MCP
tools share `people_service.People`.

- **Grid:** name (with "customer" for a customer), headline, ICP, ICP fit, notes, LinkedIn
  (the viewer's own: connected or not, "—" where Tin hasn't read it for them) and last touch.
  - The full grid names an ICP by its short label ("ICP 1"), with the heading on hover; beside
    the panel it reads in full.
  - Notes shows the start of the newest note; hovering shows it whole, with who wrote it and
    how many notes there are. Search covers every note.
  - Last touch is the latest event. LinkedIn gives no time for a reaction, so a reaction counts
    on the post's date.
  - The segments are all people, ICP fit (0.6 or more) and not connected yet, plus Invited
    once a [connection request](linkedin-requests.md) went out. The LinkedIn column reads
    "Queued" for the viewer's request waiting to go out and "Invited oct 16" for one Tin sent.
  - With the viewer's own LinkedIn reading on the project, a tick lane before the name lets
    them choose people to ask to connect (Paper REQ-A). Only people they can ask get a box:
    someone with a LinkedIn profile ID, not a customer, not their connection or invitee, and
    not asked by them before. Ticking shows a bar above the grid with the schedule and "Send N
    requests", which queues them at once (see [connection requests](linkedin-requests.md)).
    When nobody on the list has a box, a line in the bar's place says why (REQ-A4): "Everyone
    on this list is already connected to you or invited".
- **Score hover:** the reason, the score, the ICP and when it was set.
  - A score computed under an earlier `strategy/ICP.md`, or by an earlier version of the
    scoring, reads "old": the view compares each value's `definition` with the current one
    (`linkedin_engagers.read_icp`), and the next engagers run rescores it. So does a score
    older than the person's newest note.
  - A member's fit reads "you" (or "team") beside the number, and the hover gives Tin's score
    it replaced.
- **Person panel:** `GET …/people/{person_id}` adds the timeline. The address keeps the person
  (`?person=`), so a reload opens them again.
  - Each field says who set it: the workflow's short name ("engagers"), or "you" and
    "a teammate" for a member's value.
  - A reaction says whose post it was: the run read the LinkedIn of the member who started it.
- **Correcting a person:** `PATCH …/people/{person_id}`.
  - `{"fit": {"icp": "<profile>"}}` picks an ICP from the ICP file and sets the fit to 1;
    `{"fit": {"icp": null}}` is not a fit (0); `{"fit": null}` uses Tin's score again.
  - `{"customer": true}` marks a customer. The panel says Tin leaves customers out of
    invitations and reach lists, which the sending phase must honour.
  - In the panel, the ICP and Customer values open a menu.
- **Notes:** `POST …/people/{person_id}/notes`, `PATCH` and `DELETE …/notes/{note_id}` for the
  author. The panel lists them between Fields and Timeline under a box to add one; ⌘/Ctrl+Enter
  saves.
- **Forget:** `DELETE …/people/{person_id}` runs `forget` after a confirmation in the panel's
  footer. The engagers list in Files drops the person at its next run. A person forgotten while
  a run scores them is skipped, not an error.

The menu shows People where the project is offered a workflow that finds people
(`social.linkedin_engagers`, which is gated per project, or `product.visitor_journeys`), and on
`/people` itself. People a visitor journeys run named have an "On the site" segment and field;
the grid's Headline shows their last visit's summary, the panel shows their email, and a visit
in the timeline opens its replay with the suggested next step under it. Any project
member can read, correct, annotate and forget the project's people; other users get 404.

MCP gives a member's coding agent the same: `list_people` (segments, a search over notes),
`get_person`, `update_person` (an ICP, not a fit, Tin's score again, customer),
`add_person_note`, `delete_person_note` (the caller's own) and `forget_person`, which needs the
person's exact name.

## Forgetting and deletion

`forget` deletes the person and every record merged into them, with their values, events,
notes and identifiers. It keeps a SHA-256 of each identifier, salted with the project ID, so the next
read refuses them; `upsert_person` returns `None` for that person. Forgetting is per project.

Deleting a project purges all seven tables (`purge_project_data`).
`tests/test_project_purge.py` fails if a new table that holds project data is missing from
the purge.
