# Social post batch qualification

`qualification.json` proposes two article cases and one weekly case. The UUIDs are
synthetic project placeholders. For a live evaluation, use an authorized disposable
project with an ordinary article file or supplied article text; seed `social/PLAN.md`
and `context/social-updates.md` for the weekly case. Activate the matching package
revision and qualify that revision. Changing case inputs changes the qualification
digest. These cases have not been evaluated in a live Tin project. The separate
opt-in smoke test uses real services with a synthetic article and local product state;
it does not qualify these case IDs.

`tests/test_social_post_batch.py` is the article fixture suite. It checks exact source
injection, an absent style guide, one declared model call, useful rendering and
rejection of invalid excerpts, unsupported numbers or quotes, fabricated URLs,
first-person claims, repeated copy and overlong platform text. A live case cannot
force a particular bad model response, so those negative behaviors remain fixture
evidence. Human review must judge whether paraphrases really follow from the full
article; an exact excerpt and syntax checks cannot prove semantic grounding.
`tests/test_social_post_batch_weekly.py` checks edited plans, prior batch reuse,
held material, visible unfilled slots and no-new-material output without a model call.

The package drafts only. Each batch has a dated path derived from its run ID; earlier
files and human review annotations remain in place. Publishing or adding a verified
live URL is a separate decision.
