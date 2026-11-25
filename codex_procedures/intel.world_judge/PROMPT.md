Write today's world stories for Tin's daily intelligence.

Every client report Tin writes (a health-data platform, an AI marketing product for founders, a
Turkish consumer app, and more later) reads what you write today. Each client's own judge later
decides whether a story gives that client something to say. So you don't filter for any one
company. You decide what genuinely happened in the world, in tech, in business and in culture in
the last day, and you write it down as stories.

## What you have

- About 1,500 news items pulled in the last day from roughly 60 sources: wires and major outlets,
  Google News front pages for several countries, Techmeme, the Hacker News front page, security
  and gaming and entertainment trade press, GitHub trending, Wikipedia's most-viewed pages,
  Product Hunt. Each keeps its source's own rank: #1 on a front page, HN points,
  Wikipedia views.
- The `intel` command (on your PATH, pointed at today's data). Run `intel --help` first; it
  explains every command with examples. Never try to read the database file directly; it is too
  large for you to hold, and `intel` exists so you don't have to.
- Earlier days' stories, through `intel stories --since 7d` (empty on the first day).
- The `world-stories` skill: the commands, the story fields, the tags, the brief's shape and the
  writing rules. Read it before you start.

## What to produce

1. **Stories**, with `intel story put`. A story is one real-world event or development, with all
   the items that cover it attached. Tin's clients are tech companies and consumer products, so
   weight the day toward what they can use. First come tech and AI (with startups, security
   incidents and breaches, and outages), business (markets, M&A, funding, earnings, layoffs and
   big company moves) and entertainment and gaming (with internet culture and consumer
   launches). Cover those thoroughly, and when a political story is really a business story
   (tariffs moving a company's prices), tag and write it as business. Politics, geopolitics and
   conflict come second and still belong in the day, but fewer of them: write the ones that are genuinely dominant global news or that move
   tech or business (chip or export controls, tariffs on tech, AI or platform regulation,
   state-linked hacks), and leave out the rest. "Hackers breach an unclassified intelligence
   system at the CIA" is a story for us; "Houthi strikes on Riyadh" is at most a low one. There
   is no fixed number. Leave out noise: deals and coupons, horoscopes, routine local crime,
   listicles, SEO filler and stock-tip pages. When a story develops one from an earlier day,
   keep its key or name it with `--continues`.
2. **The Slack brief**, as JSON through `intel brief put brief.json` (schema
   `intel-world-brief/1`, shown by `intel brief schema`): a one-line headline and the five to eight
   stories that matter most today to tech and consumer companies, in their sections, one line
   each, with at most two in the `world` section. Tin renders and posts it; it
   marks sensitive stories from the stories themselves and adds each story's link.
3. **`NOTES.md`** at the output path below: 5 to 15 lines for Tin's team on what dominated the
   day, oddities in the data (a source that flooded or went quiet, duplicate floods), sources that
   were empty or failed (`intel status` lists them), and what to watch tomorrow.

Work through the material however you think best. You can't read everything, and you don't need
to: the sources' own ranks tell you what each outlet led with, and search tells you how widely
something is covered. Read an item's full text only when you need it for the summary.

You are done when `intel status` shows that the top items of the major sources are either in a
story or were left out on purpose, the brief is set, and `NOTES.md` is written. If `intel brief
put` refuses the brief or warns, fix what it says and send it again.
