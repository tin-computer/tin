# Social post batch qualification

`qualification.json` proposes two positive cases for approved `content.generate`
articles. The UUIDs are synthetic placeholders, not existing runs. For an authorized
live evaluation, create and approve the appropriate article runs in the evaluation
project, put their actual UUIDs in the case file, then qualify that exact revision.
Changing those inputs changes the qualification digest. Activate the matching package
in the evaluation project before running cases. These cases have not been evaluated
in a live Tin project. The separate opt-in smoke test uses real services with a
synthetic article and local product state; it does not qualify these case IDs.

`tests/test_social_post_batch.py` is the offline fixture suite. It checks exact source
injection, an absent style guide, one declared model call, useful rendering and
rejection of invalid excerpts, unsupported numbers or quotes, fabricated URLs,
first-person claims, repeated copy and overlong platform text. A live case cannot
force a particular bad model response, so those negative behaviors remain fixture
evidence. Human review must judge whether paraphrases really follow from the full
article; an exact excerpt and syntax checks cannot prove semantic grounding.

The package drafts only. The report's order is a suggestion, not a scheduled post,
and no live article URL is supplied. Publishing or adding a verified live URL is a
separate decision.
