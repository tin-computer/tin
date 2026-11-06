# AI Visibility

The AI Visibility page shows how often AI answer engines name and recommend a project when people
ask the project's panel of buyer questions, one week at a time. It appears in a project's
navigation once the project has a stored week.

## What a week holds

A week's report lists the panel's questions in groups and sections, the answer engines, and every
answer each engine gave to each question (usually two a week). Each answer is judged as one of:

- `recommended`: it presents the project as an option for the asker;
- `mentioned`: it names the project, or cites a page on the project's site, without recommending it;
- `none`: neither;
- `no_answer`: the engine gave no answer (for example, Google showed no AI Overview).

An answer also keeps the quote that names the project, the engine's search queries when the
engine reports them, the sources it cited, the pages it read without citing (some engines cite
nothing inline), and the brands it recommended instead.

Engines are of two kinds, scored apart:

- `web`: what people see today, meaning AI apps as a logged-out user gets them, and models asked
  through their APIs with web search on;
- `memory`: models with web search off, which shows what they learned in training.

A question with `in_score: false` stays in the table but out of every score, such as a question
that names the project or one that only checks citations.

## The page

- **Score:** a sentence that says the week (how many answers recommended the project and how
  many named or cited it, how many questions brought it up, who was recommended instead most),
  then four tiles: the share of the kind's answers that recommended the project, the share that
  mentioned or cited it (recommendations included), how many questions named it at least once,
  and the brand recommended most often instead. Each share shows its change from the week before.
  The Recommended tile turns moss when its share rose by 3 points or more on the week before and
  crimson when it fell as far; a smaller move, which a few hundred answers a week can't tell from
  noise, or a first week leaves it plain.
- **Week by week:** once there are two weeks, both shares for every week. Selecting a week opens
  it, as do the arrows by the week's dates.
- **Table:** questions down the left in their sections and groups, an engine in each column, and
  each question's three brands most often recommended instead. A cell shows the best of its
  answers as a chip: a tick on moss where an engine recommended the project, a dot on sand where
  it only named or cited it, a faint cross where it did neither, and a dash for no answer, with a
  `1/2` tag when only one of two answers reached it. Selecting a cell opens that engine's answers
  under the row.

## Storing a week

Reports are produced outside Tin for now and an operator imports each week:

```bash
uv run tin-lite import-ai-visibility --project-id <project uuid> --file week.json
```

The command validates the report (version 1, see `src/tin_lite/ai_visibility.py`) and stores it in
`ai_visibility_weeks`, keyed by project and the week's Monday. Importing the same week again
replaces it. The page and its API read only that table:

- `GET /api/projects/{id}/ai-visibility?week=YYYY-MM-DD` returns one week (any day in it, the latest
  when left out), with its score and every stored week's totals;
- `GET /api/projects/{id}/ai-visibility/answers?week=…&question=…&engine=…` returns one cell's
  answers.

Both require project membership. Nothing here runs on a schedule, calls a provider or charges
credits.
