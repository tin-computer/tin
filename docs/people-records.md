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
| `record_events` | The timeline: `reacted`, `commented`, `viewed`, `invited`, `accepted`, `withdrawn`, `visited` |
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
- `icp_fit` (1 not a fit, 2 weak fit, 3 good fit, 4 strong fit; migration 086 turned the earlier
  0 to 1 scores into these by their bands) and `intent` (`hot`, `warm` or `cold`: how much
  interest they show now), both from the shared ICP rating (`people_rating`), each with its
  reason. Intent cools when read (`cooled_intent`): with nothing new from the person (a visit,
  reaction, comment or profile view), hot reads as warm after 7 days and anything as cold after
  30. The stored rating is unchanged; the panel says what Tin rated and since when it's quiet,
  and their next activity gets them rated again;
- `customer` (`yes` or `no`), which only members set;
- `web_tier` (`lead`, `new_user` or `user`: what they were on the site when visitor journeys last
  read them, with where they stopped as the reason) and `web_summary` (that run's one-line
  summary). A visitor journeys run sets `name` only when no other source has.
- `web_source` (`ad`, `organic`, `ai`, `social`, `referral` or `direct`): how their last visit
  arrived, from its UTM tags, ad click IDs and referrer, with the source as the reason
  ("Google Ads, keyword “epic data warehouse”").
- `phone`: the numbers ContactOut has on file for a good or strong lead, comma-separated, with
  their count as the reason (`lead_phones`, see [RB2B webhook](rb2b-webhook.md#phone-numbers-for-leads)).
  People's panel shows each as a call link.

A person no source gave a company gets one from what Tin has (`records.fill_company`): their
headline's "Title at Company" or "Title @ Company" (the part before a `|`, `·` or `•`; not a
lowercase phrase, a long sentence or a placeholder like "Retired"), else a work email's domain.
It runs on any run's write of someone without a company, set by `people.company` with the
reason "From their headline" or "From their email address", and follows a changed headline. A
source that names the company replaces it, a member's is never replaced, and the LinkedIn
connections export still reads the profile of anyone whose position came only from a headline.
Name, headline, company, position and location are kept unescaped ("R&amp;D" reads "R&D").

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

The ICP rating sends a person's five newest notes with their fields and what they did, and the
instructions weigh a note above everything else. A note newer than a rating, or something new
the person did, makes the rating stale: the view marks it, and `unscored` picks the person up
again. A fit a member set is never sent to the model. Editing the ICP file doesn't: only a new
scorer version or another ICP file marks ratings old (`people_records.rubric`), so routine
edits never rate everyone again.

## Events

Events are only added. Each has an `event_key` that is unique per project: an engagement
keys on the post, the person and the kind, so reading the same post again adds nothing. A
profile view keys on the member, the person and the day, and a view seen again within the span
LinkedIn's "… ago" leaves open adds nothing either. A `visited` event keys on the person and the
run's window end date, so a retried or overlapping run adds one a day; it holds the summary,
where they stopped, the suggested action and a PostHog replay link. An event written to a record
that later merged counts under the survivor.

## The People view

`/people` lists a project's people for its members (`src/tin_lite/static/people.js`, Paper
boards PEOPLE-A1, C1, C1b, E1–E3, N1 and N1b). It reads `GET /api/projects/{project_id}/people`
(`people_records.directory`), every person up to a 30,000 runaway guard, with the ICP file's
profiles; the page sorts, searches, segments and pages them, fifty at a time. Past the guard,
people with a score, a visit, a note or an event besides `connected` come first, so a member's
imported connections never push visitors out. `people_records.people` reads each part once for
the whole list (no per-person scans), so ten thousand connections list in about two seconds. The HTTP routes (`people_api.py`) and the MCP
tools share `people_service.People`.

- **Grid:** name, company, headline, ICP, Fit, intent ("🔥 hot", "🌤 warm", "🧊 cold";
  sorted hot first, then people without one), came from and last touch. Notes live in the
  panel and in search. A visit's last touch is its day alone; the segment and Came from say what it was.
  - The name opens the person's LinkedIn profile in a new tab, when Tin knows it; the rest of
    the row opens them in the panel. The row is one keyboard stop (Enter or Space opens the
    person) and the name link the next. Beside the name: "customer", or the viewer's request on
    its way: "queued", then "invited oct 16".
  - The full grid names an ICP by its short label ("ICP 1"), with the heading on hover; beside
    the panel it reads in full.
  - Last touch is the latest event and who was behind it (Paper TEAM-A): "reacted to you",
    "reacted to Mira", "viewed you", "invited by Mira", "connected with you", "connected to
    Emre" (a teammate's own connection; "a teammate" without a name), "new connection of you"
    and "starred on GitHub";
    a visit is its day alone. LinkedIn gives no time for a reaction, so Last touch shows it on
    the post's date; intent cooling counts it from when Tin saw it. A profile view counts on
    the day LinkedIn's "3d ago" gives.
  - The segments are Visitors (anyone with a `visited` event or an "On the site" value, which
    includes people found for a visit only their company made), Hot (intent hot), Social
    (reacted, commented, viewed a member's profile, became a member's new connection or
    starred the project's repository), all
    people, Connections, ICP fit (3 or more) and not connected yet, plus Invited once a
    [connection request](linkedin-requests.md) went out and From ads. Visitors, Hot, Social and
    Connections show only when someone is in them.
  - Connections holds a member's LinkedIn connections brought in by a bulk import (the
    connections export workflow or a CSV, `records.BULK_SOURCES`) who haven't done anything Tin
    saw (a visit, a reaction, a comment, a profile view, a new connection, a star): `imported`
    on the directory. A new connection is someone who connected with the member after the
    connections export first read their list (a `new_connection` event, see
    [connections](linkedin-connections.md#new-connections)); everyone connected before then
    stays here.
    Every other segment, All people included, leaves them out, and Tin buys nothing for them
    (no phone lookup). Once one does something, they move out of Connections. The page opens on Visitors when anyone has visited, else on
    all people.
  - With the viewer's own LinkedIn reading on the project, a tick lane before the name lets
    them choose people to ask to connect (Paper REQ-A). Only people they can ask get a box:
    someone with a LinkedIn profile ID, not a customer, not their connection or invitee, and
    not asked by them before. Ticking lays a bar over the grid's pinned header row (PJ-A) with
    the schedule and "Send N requests", which queues them at once (see
    [connection requests](linkedin-requests.md)). When nobody on the list has a box, a line in
    the footer beside the count says why (REQ-A4): "Everyone on this list is already connected
    to you or invited". Neither moves the grid.
- **Score hover:** the reason, the rating ("4 · Strong fit"), the ICP and when it was set.
  - A score computed by an earlier version of the scoring, or under another ICP file path,
    reads "old": the view compares each value's rubric (`people_records.rubric`: the path and
    scorer version, not the file's words) with the current one, and the next run that rates
    people rates it again. Editing `strategy/ICP.md` marks nothing old. So does a score older
    than the person's newest note.
  - A member's fit reads "you" (or "team") beside the number, and the hover gives Tin's rating
    it replaced.
- **Person panel:** `GET …/people/{person_id}` adds the timeline, `position`, `location` and
  `way_in`. The address keeps the person (`?person=`), so a reload opens them again.
  - Under the headline: position, company and location, leaving out what the headline already
    says, and "Found for a company visit" for someone found at a company whose visit RB2B
    matched only to the company (`visited` events with `source: rb2b_company` and no visit of
    their own).
  - **Way in:** the same 🤝 line the [RB2B Slack message](rb2b-webhook.md) uses, computed
    server-side with `warm_paths.known` and `line`: who on the team is connected to them, or
    knows someone at their company, by first name, "You" for the viewer, "Your team" without a
    name. A second-degree path a past RB2B run of this project found (its `{run_id}:rb2b:warm`
    receipt) shows when there's no direct connection. It reads People and receipts only, never
    LinkedIn, and only this project's members and runs. No path, no row.
  - From 1240px wide the grid narrows beside the panel; below that the panel floats over the grid,
    so opening someone moves nothing, and on a phone it fills the screen.
  - Each field says who set it: the workflow's short name ("engagers"), or "you" and
    "a teammate" for a member's value.
  - A reaction says whose post it was: the run read the LinkedIn of the member who started it.
  - The timeline and notes name teammates too (Paper TEAM-C): "Reacted to Mira's post",
    "Viewed Mira's profile", "Mira sent a connection request". Names are each teammate's first name from their Tin account
    (Clerk), read when the view is built; without one, "a teammate". The service turns member
    IDs into `who` ("you", a first name or "a teammate") before anything leaves Tin.
- **Correcting a person:** `PATCH …/people/{person_id}`.
  - `{"fit": {"rating": 3}}` sets the fit (1 to 4); Tin's profile and intent stay.
    `{"fit": null}` uses Tin's rating again.
  - `{"customer": true}` marks a customer. The panel says Tin leaves customers out of
    invitations and reach lists, which the sending phase must honour.
  - In the panel, the ICP fit and Customer values open a menu.
- **Notes:** `POST …/people/{person_id}/notes`, `PATCH` and `DELETE …/notes/{note_id}` for the
  author. The panel lists them between Fields and Timeline under a box to add one; ⌘/Ctrl+Enter
  saves.
- **Forget:** `DELETE …/people/{person_id}` runs `forget` after a confirmation in the panel's
  footer. The engagers list in Files drops the person at its next run. A person forgotten while
  a run scores them is skipped, not an error.

The menu shows People where the project is offered a workflow that finds people
(`social.linkedin_engagers` or `social.linkedin_profile_viewers`, both gated per project, or
`product.visitor_journeys`), and on `/people` itself. People a visitor journeys run named have an
"On the site" field (and are in Visitors); once any visit's source is known the grid adds a sortable
"Came from" column (descending reads ads first) and a "From ads" segment, and MCP's
`list_people` returns `came_from` and takes the `from_ads` segment. The grid's Headline shows their last visit's summary, the panel
shows their email, and a visit in the timeline opens its replay with the suggested next step under
it. Any project
member can read, correct, annotate and forget the project's people; other users get 404.

MCP gives a member's coding agent the same: `list_people` (segments including `visitors` and
`hot`, a search over notes and companies, each row's `company`, `visited`, `company_visit` and
`way_in` from People),
`get_person`, `update_person` (a fit of 1 to 4, Tin's rating again, customer),
`add_person_note`, `delete_person_note` (the caller's own) and `forget_person`, which needs the
person's exact name.

## Forgetting and deletion

`forget` deletes the person and every record merged into them, with their values, events,
notes and identifiers. It keeps a SHA-256 of each identifier, salted with the project ID, so the next
read refuses them; `upsert_person` returns `None` for that person. Forgetting is per project.

Deleting a project purges all seven tables (`purge_project_data`).
`tests/test_project_purge.py` fails if a new table that holds project data is missing from
the purge.
