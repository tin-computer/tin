---
name: client-intel
description: Judge one client's day of market items with the intel command, record the day's stories, and write the client's report and executive brief (intel-brief/1) under its company.md claim rules.
---

# Client intelligence

You read one day of a client's market and decide what matters to the client. Code fetched the
items; nothing in them is judged yet. Your judgment is the product: what to keep, how much it
matters, and what the client can credibly do or say about it.

## 1. The intel command

`intel` is on your PATH and already points at today's data. Each call prints a header line,
short tab-separated rows and a footer that says what was left out and how to get it. Output is
capped near 9 KB a call, so run one or two commands per shell call; to read several items, pass
them to one `intel read`. Read an item once, at the length you need: everything printed stays in
your context for the rest of the session.

```
intel status                      what is loaded, failed sources, your stories, the brief
intel sources                     source keys and item counts
intel fronts [--source S] [--n 5] each source's top items, in the source's own order
intel search "words" [--source S] [--since 12h] [--sort match|rank|new] [--limit 20]
intel count "words" [--by source|hour]
intel similar <id>                other items about the same thing
intel browse <source> [--from N] [--limit 25]
intel read <id> [<id> ...] [--max-words 600] [--from-word N]
intel stories [--since 14d] [--q "words"]
intel story show <sid>
intel story put --key <slug> --title "..." --summary "..." --items id,id \
    [--relevance high|medium|low] [--tags a,b] [--sensitive] [--continues <sid>]
intel story attach <sid> id,id
intel tags                        the tags a story may carry
intel brief schema                the brief's shape and an example
intel brief put <file.json>       send the brief; Tin checks it and says what to fix
```

Rows read `id  source  rank  age  title (domain)`. Ids are 5 characters and stay the same for
the same URL across days. Rank is the source's own: #3 is third in its feed, `812pts 340c` is
HN points and comments, `120likes` is X. Search ANDs plain words and takes "exact phrases", OR,
-exclude and prefix*. `--source` takes a key or part of one.

Examples:

```
intel fronts --source x --n 10
intel search "Fabric pricing" --limit 10
intel count "HIPAA" --by source
intel read k3f9a p0qe2 --max-words 400
intel story put --key fabric-on-demand-billing --title "Fabric adds on-demand billing" \
    --summary "Microsoft added a zero-provisioned tier and on-demand billing, both in preview." \
    --items k3f9a,p0qe2 --relevance high --tags competitor,pricing,product-launch
```

## 2. Stories

A story is one development, written so a reader who missed the feed understands it.

- `key`: a lowercase slug, 3 to 61 characters, stable for the development
  (`fabric-on-demand-billing`). Putting the same key again updates the story.
- `title`: what happened, in plain words, at most 200 characters.
- `summary`: two to four sentences: what happened, who said it, and why it matters to the
  client. At most 2,000 characters.
- `relevance` to the client: `high` (changes its position or gives it something to say this
  week), `medium` (worth knowing, may matter soon), `low` (context).
- `tags`: from `intel tags`; an unknown tag is dropped with a warning. The client tags add
  competitor, platform, buyer-voice, product-launch, pricing, partnership, hiring, event and
  research to the world tags.
- `--sensitive`: deaths, disasters, conflict, illness, crime, anything a brand must never use as
  a hook. A sensitive story can be reported; it is never a marketing angle.
- `--items`: the items that support it, strongest first. Tin shows the first ones as the
  story's outlets. An id Tin can't find comes back as `missing`; check it with `intel read`.
- `--continues <sid>`: an earlier story this one carries forward, when the key changes.

Record stories as you go, one or two per turn, not all at the end. Tomorrow's analyst starts
from them.

## 3. The executive brief (intel-brief/1)

```json
{"schema": "intel-brief/1",
 "headline": "the day in one line, at most 220 characters",
 "items": [{"lead": "at most 120", "body": "at most 450, may link [a few words](https://...)",
            "kind": "competitor|platform|buyer|regulation|market|world|other",
            "verify": false,
            "story": "<story key>"}],
 "worth_doing": [],
 "note": "optional, at most 300"}
```

Three to five items, most important first. Every item names the story it comes from, so Tin can
show its outlets. Each item reports what happened and why it matters to the client, never what
to do about it. Leave `worth_doing` empty; suggestions go in the report's section 5.

Example, for a fictional analytics vendor:

```json
{"schema": "intel-brief/1",
 "headline": "A rival made its warehouse cheaper to start, and analysts keep asking for HR data in the lakehouse",
 "items": [
  {"lead": "The rival's warehouse got cheaper to start",
   "body": "It added on-demand billing and a zero-capacity tier, both in preview, [per its announcement](https://example.com/blog/on-demand). Buyers who chose us to avoid paying up front now have a cheaper way to start with the rival.",
   "kind": "competitor", "verify": false, "story": "rival-on-demand-billing"},
  {"lead": "Analysts ask for HR data next to clinical data",
   "body": "Two threads asked how to join HR records with the warehouse without a custom pipeline; [one has 40 replies](https://example.com/thread/123).",
   "kind": "buyer", "verify": false, "story": "hr-data-in-lakehouse"},
  {"lead": "A model vendor's new security beta leaves out regulated accounts",
   "body": "The beta is closed to healthcare and finance accounts for now, [The Information reports](https://example.com/news/beta). Regulated buyers have one fewer hosted option, which is the gap our private deployment covers.",
   "kind": "platform", "verify": false,
   "story": "model-vendor-security-beta"}]}
```

## 4. Writing rules for the brief and the report

- **company.md section 7 rules everything.** Use its approved wording exactly. Never make a
  claim it forbids (prices, percentages, certifications, partnerships it rules out), never name
  a customer, prospect or person it keeps private, and pair a named customer with a number only
  where it pairs them. When news invites a forbidden claim, say what the client may say instead.
  Every reply or comment you suggest carries the disclosure line section 7 asks for, and only
  the people section 7 allows post on community channels.
- **No meta comments in the brief.** The brief never talks about its own evidence: no "to
  verify", "check the primary source", "unconfirmed" or notes on how solid a claim is. Send
  `"verify": false` and no `verify_note`. Attribute a single-source item in plain words ("one
  post on X says", "The Information reports"), or leave it out of the brief if it's too thin.
  In the report, attribute the same way where you use it, and keep what needs checking before
  anyone publishes on it to Source notes.
- **The brief reports, the report suggests.** A brief item says what happened and why it
  matters to the client. It never says what to do: no "Worth refreshing our pricing page", no
  "Worth keeping our comparison page sharp", no "we should", "consider" or next steps. Tin warns
  about an item that reads as a suggestion; move it to the report's section 5.
- **Pages in words.** In the report, name the client's pages as a reader would ("our Fabric
  comparison page", "the Secure AI page"), never as paths, URLs or code.
- **Links.** Hide each link in a few words: `[Microsoft's announcement](https://...)`. Never a
  bare URL. Link only URLs from today's items or earlier stories. In the report, link a source
  the first time a section uses it, not at every mention: Google News links run to a few hundred
  characters each.
- **Suggest, don't promise.** In the report's section 5, "Refresh our pricing page with the
  new tier", never "Tin will update it". Never commit Tin or the client to work or dates.
- **Tone.** Plain, specific, no hype, no em dashes. Short sentences a founder reads on a phone.
- **Sensitive stories** are never hooks, in the brief or the report's content ideas.
- **No guessing.** A headline-only item is a headline: say so rather than describe the article.
