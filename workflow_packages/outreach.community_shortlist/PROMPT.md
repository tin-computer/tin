# Build a community reply shortlist

Follow the community-shortlist skill. Use the open browser to search public forums and
communities for threads where someone is describing the problem in `problem_statement`,
posted within `lookback_days`. Work from the problem, never from the product name: do not
search for the product, a competitor, or any brand term.

Produce one reviewable shortlist at the declared output path. This is research and drafting
only: do not post, comment, reply, message, vote, or create any account anywhere. Do not
include a reply that names, links, or promotes the product in a community whose rules (or the
supplied `house_rules`) forbid it; mark that row `hold` instead of `review` and say why.

Every included thread must be a real, currently reachable page you visited during this run.
Never invent a thread, url, or quote. If fewer than `max_threads` genuine matches exist, return
fewer rows rather than padding the list.
