# Pull request body and receipt

Use these headings, in this order, for both the pull request body and the receipt at
`reports/framework-starter/{run_id}/RESULT.md`. A no-change result uses the same headings, with
`Outcome` naming the reason from `choose_starter` or `check_starter` and the sections that
describe the starter saying none. Keep the receipt under 18,000 bytes so it fits as a
`content.plan` context file; it holds no starter code, only paths.

## Outcome

`patch` or `no_change`, the repository, its revision, and one sentence: which developer can now
clone what, in which framework, to do what. For no-change, the reason and what would change it.

## What a developer gets

The starter's directory, the capability it shows, and its run command.

## Grounded in

A table of every product call the starter makes: the call, what it does, and the repository
`path:line` that defines it. Then the FRAMEWORKS.md facts with their evidence, and the earlier
starters that ruled out other frameworks.

## Secrets

Each variable in `.env.example`, whether it is server-only or public, and the file that reads
it.

## Verification

The `check_starter` result, the checks actually run and their results, then the checks not run
and why. Always include the manual step: from a fresh clone, follow the README with a real key
and see one successful result.

## Next steps

- Merge, then link the starter from the product's docs and README quickstart.
- For Next.js: after merge, the README's deploy button works; submitting to Vercel's template
  gallery is the founder's call, under Vercel's current rules.
- Add this receipt to `content.plan` as a context file to plan a tutorial article that builds
  on the starter.
- If the starter moves to its own public repository, run `outreach.awesome_lists` with that
  repository's URL to find the framework lists that would accept it.

## Not changed

What was deliberately left alone: everything outside the starter directory, the product's own
code, docs and CI, and any framework `choose_starter` ruled out.

End the receipt with this block, which the next run reads back:

````markdown
```json framework-starter
{"version": 1, "outcome": "patch", "framework": "nextjs", "path": "examples/nextjs-starter", "use_case": "<one line>", "calls": ["<call>"]}
```
````

A no-change receipt uses `"outcome": "no_change"` with `framework` and `path` set to `null`.
