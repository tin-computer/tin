---
name: community-replies
description: Find live public threads where someone describes the problem this product
  solves, and draft an honest, non-promotional reply for founder review.
---

Search Reddit and Hacker News (and any communities named in the input) for recent threads
where someone is describing the problem in `problem_description` in their own words —
not threads mentioning competitors by name, and not the product itself.

For each thread found, up to max_threads:
- Record the exact thread URL, subreddit/site, and a short quote of the person's actual words
  (do not paraphrase into something they didn't say).
- Judge genuine fit: would a normal, non-marketing person reasonably reply here? Skip threads
  where the fit is a stretch.
- Draft a reply that helps first and mentions the product only if it's earned, in one line,
  with no link unless the community's own norms clearly allow one. Flag communities with
  strict self-promotion rules so the founder edits before posting.

Never post, comment, vote, or message. Never invent a thread that does not exist. If fewer
than max_threads good-fit threads exist, report fewer rather than lowering the bar.
Write only the declared output.