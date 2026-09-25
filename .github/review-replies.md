# Replies for closing workflow pull requests

Maintainers: when a workflow pull request passes the automatic gate but won't be merged,
close it with one of these and fill in the specifics. Always name what it overlaps with or
what would change the answer. Authors of #107 and #124 asked which workflow theirs
duplicated and got no answer.

**Too similar**
> Thanks for running this on your project. It covers the same job as `<key or #PR>`:
> `<one line on the overlap>`. We're closing it to keep one workflow per job. If you see
> something that one misses, a change to it is welcome.

**Too manual**
> The founder would have to supply `<input>` by hand, and Tin already has `<source>` for
> that. A version that reads it from `<onboarding / Code map / integration>` and
> `<does the step itself>` would be worth another look.

**Too niche**
> This depends on `<setup or data>`, which most Tin projects don't have, so few founders
> could run it. `<Optional: the upstream step that would make it broadly useful.>`

**Out of scope**
> Tin's workflows cover marketing: getting found, chosen and paid. `<topic>` falls outside
> that for now.

**Not enough value yet**
> The run shows `<what it produced>`, which a founder could get from `<existing workflow or
> a single prompt>`. We'd need `<what would make it worth running repeatedly>`.

**Tries to do too much**
> This combines `<job A>` and `<job B>`. Pick the one your run showed was most useful and
> send that on its own.
