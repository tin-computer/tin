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
them to one `intel read`.

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

Record stories as you go, not at the end. Tomorrow's analyst starts from them.

## 3. The executive brief (intel-brief/1)

```json
{"schema": "intel-brief/1",
 "headline": "the day in one line, at most 220 characters",
 "items": [{"lead": "at most 120", "body": "at most 450, may link [a few words](https://...)",
            "kind": "competitor|platform|buyer|regulation|market|world|other",
            "verify": false, "verify_note": "at most 160, when verify is true",
            "story": "<story key>"}],
 "worth_doing": [],
 "note": "optional, at most 300"}
```

Three to five items, most important first. Every item names the story it comes from, so Tin can
show its outlets. Leave `worth_doing` empty; suggestions go in the report.

Example, for a fictional analytics vendor:

```json
{"schema": "intel-brief/1",
 "headline": "A rival made its warehouse cheaper to start, and analysts keep asking for HR data in the lakehouse",
 "items": [
  {"lead": "The rival's warehouse got cheaper to start",
   "body": "It added on-demand billing and a zero-capacity tier, both in preview, [per its announcement](https://example.com/blog/on-demand). That weakens the pay-up-front point on our comparison and pricing pages.",
   "kind": "competitor", "verify": false, "story": "rival-on-demand-billing"},
  {"lead": "Analysts ask for HR data next to clinical data",
   "body": "Two threads asked how to join HR records with the warehouse without a custom pipeline; [one has 40 replies](https://example.com/thread/123).",
   "kind": "buyer", "verify": false, "story": "hr-data-in-lakehouse"},
  {"lead": "A model vendor's new security beta may exclude regulated accounts",
   "body": "So far only secondary coverage says so, [in one report](https://example.com/news/beta). If the vendor's own docs confirm it, it supports our private deployment page.",
   "kind": "platform", "verify": true,
   "verify_note": "One secondary report; check the vendor's own documentation.",
   "story": "model-vendor-security-beta"}]}
```

## 4. Writing rules for the brief and the report

- **company.md section 7 rules everything.** Use its approved wording exactly. Never make a
  claim it forbids (prices, percentages, certifications, partnerships it rules out), never name
  a customer, prospect or person it keeps private, and pair a named customer with a number only
  where it pairs them. When news invites a forbidden claim, say what the client may say instead.
  Every reply or comment you suggest carries the disclosure line section 7 asks for, and only
  the people section 7 allows post on community channels.
- **To verify.** Anything resting on a single secondary or unverified source is marked: in the
  brief `"verify": true` with a `verify_note` saying what to check; in the report, say so where
  you use it and list it in Source notes.
- **Pages in words.** Name the client's pages as a reader would ("our Fabric comparison page",
  "the Secure AI page"), never as paths, URLs or code.
- **Links.** Hide each link in a few words: `[Microsoft's announcement](https://...)`. Never a
  bare URL. Link only URLs from today's items or earlier stories.
- **Suggest, don't promise.** "Worth refreshing our pricing page", never "Tin will update it".
  Never commit Tin or the client to work or dates.
- **Tone.** Plain, specific, no hype, no em dashes. Short sentences a founder reads on a phone.
- **Sensitive stories** are never hooks, in the brief or the report's content ideas.
- **No guessing.** A headline-only item is a headline: say so rather than describe the article.
