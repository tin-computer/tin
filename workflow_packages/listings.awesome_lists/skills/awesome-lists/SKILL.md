---
name: awesome-lists
description: Find curated GitHub lists that would accept the product, check each list's own written rules, and prepare a paste-ready submission for the best ones.
---

# Awesome lists

Curated "awesome" lists on GitHub are read by people who are choosing tools, and they change only
through pull requests that follow each list's own written rules: open source only, a minimum
number of stars, alphabetical order, a one-line description, a checklist in the pull request
template. Most submissions fail on a rule the author never read. This skill finds the lists where
the product would be accepted and prepares the submission, so the founder only reviews and sends it.

Run it when the product launches, ships a major release, adds a license or a public repository, or
about once a quarter, because new lists appear and old ones go quiet.

## The rule behind the design

A list is a gate with written specs. Check the specs before spending any effort, drop what fails,
and keep the reason. The rejected lists are as useful as the accepted ones: they show which real
change to the product would open the most gates.

## Stages

Each stage has a feed, a spec, and a reject stream that you keep and count. The counts go in the
report as a funnel.

### Stage 0. Read the product

Sources: the product URL, the repository if given or linked from the site, and project files.

1. Record the facts a list can test: open source or not, license name, repository URL, stars,
   age of the repository, latest release date, main language or platform, and the category in
   plain words.
2. Split them into **fixed facts** (stars, age, closed source) and **fixable facts** (a missing
   license file, no README usage section, no docs link, no topics, no changelog).
3. Write two entry lines. Each is one neutral sentence of under 15 words that says what the
   product does. No superlatives, no words like best, powerful or revolutionary, no claim the
   product's own pages do not show. Lists reject marketing language.

### Stage 1. Source lists

Search GitHub for "awesome" plus the category, and use topic pages and lists that link to related
tools. Include lists in adjacent categories where the product fits a section. For each list, open
the README, the contributing guide and the pull request template if there is one. Open at most
about 30 lists and keep a short list of the queries you used.

Reject and count: not a curated list, or a list of one company's own projects.

### Stage 2. Gate A: is the list alive

Pass only if all hold:
- the repository is not archived and the README is not marked deprecated;
- the last commit is within 12 months;
- at least two pull requests from people who are not maintainers were merged within 12 months
  (check the closed pull requests page).

Also note a stalled queue: outside pull requests that have waited over 6 months with no recent
merges. Reject stalled and dead lists, and count them.

### Stage 3. Gate B: does the product meet the list's own rules

Write down every inclusion criterion the list states in its README, contributing guide or pull
request template. Test each against the product evidence as Pass, Fail or Unknown, with the URL of
the page each rests on. Never guess: Unknown means the pages do not say.

- Any Fail on a hard criterion removes the list. Keep the rule (paraphrased) and its URL.
- Search the list's README for the product name and repository URL. If it is already listed, drop
  the list and say so.
- Unknown criteria keep the list in, marked "check before submitting".

Then aggregate the rejections. Which single **fixable** fact, if changed, would move the most
rejected lists to eligible? Report up to three such fixes with the count and the list names. Fixed
facts such as stars and age are never presented as something to work around. Suggest only real
changes.

### Stage 4. Score

Score each eligible list out of 7 and show the arithmetic:
- Reach, 0 to 2: 2 if the list repository has 10,000 or more stars, 1 for 1,000 to 9,999, 0 below
  that. If the star count is not on a page you opened, score 0 and say so.
- Fit, 0 to 2: 2 if the product belongs in an existing section, 1 if adjacent. If a new section
  would be needed, note it, since many lists require several entries before adding a category.
- Acceptance, 0 to 2: from Gate A evidence. 2 for three or more outside pull requests merged in
  12 months, 1 for one or two, 0 otherwise.
- Effort, 0 to 1: 1 if an entry is a single line that follows a clear pattern, 0 if the list needs
  extra material such as screenshots, coverage links or several pull requests.

Rank by total and break ties by Acceptance.

### Stage 5. Submission packets for the top five

For each list give:
- the exact section heading and where in it the line goes (alphabetical, by date, at the end);
- the entry line, matched to its neighbours: link style, separator, capitalization, trailing
  punctuation, and badges only if the list uses them;
- the pull request title and commit message convention, from the contributing guide or from
  recently merged outside pull requests;
- each box of the pull request template with the honest answer and its evidence;
- a note that the founder submits from their own account and says they maintain the product.

Do not fork, open pull requests or issues, or comment. Nothing is sent.

### Stage 6. Verdict

One of: "Submit these N lists this week, in this order", "Fix this first, then run again", or "No
list accepts this product yet". Lead the report with it.

## Honesty rules

- Never list a list you did not open. Never invent a star count, date, rule, section or format.
- Paraphrase rules and list text. At most one short quote, under 15 words, per page.
- Never suggest buying stars, inflating activity, or any way of appearing to meet a rule.
- Do not claim the product has a feature its own pages do not show.
- The output is a ranked set of submissions. Do not predict traffic, stars or signups.
- If fewer than three lists pass, say so and show the funnel. Do not lower a gate.
- Put everything you could not open or verify under "Evidence limits".

## Output

Write reports/AWESOME_LISTS.md using REPORT_TEMPLATE.md. Keep it under 25,000 characters.