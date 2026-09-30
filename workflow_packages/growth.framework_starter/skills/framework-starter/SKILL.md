---
name: framework-starter
description: Add one small runnable starter for a developer framework to the product's repository, calling only what the repository defines, as one reviewable GitHub pull request.
---

Developers pick up a tool when they start something new, and they start from a working example
rather than from reference docs. A starter that runs in five minutes, in the framework they
already use, is the shortest path from "heard of it" to "built on it". This run writes that
starter into the product's own repository, where the founder can merge it, link it from the
docs and point a deploy button at it. It is only for products a developer integrates; for
anything else the right result is a short no.

1. **Read the context.** In the project state, `/home/user/state`, read when present:
   - `wiki/INDEX.md`, `### Code map`: where the public API, SDK, webhooks or embed live, as a
     map for step 2, not as proof;
   - `wiki/INDEX.md`, `### Feature map`: which capability shows the product's value fastest;
   - `reports/GROWTH_ONBOARDING_PLAN.md`: the developer the product is for, and its hard no's,
     which are binding;
   - `.agents/skills/writing-style/SKILL.md`: the voice for the starter's README;
   - every `reports/framework-starter/*/RESULT.md`: its `framework-starter` JSON block names
     the framework and path of each earlier starter.
   Read Tin's open pull-request evidence too: an open PR that adds a starter counts as earlier.

2. **Find what a developer calls.** In the repository, `/home/user/project`, find the published
   client (package manifests, exports, `__init__.py`), the public API (routes, an OpenAPI or
   GraphQL schema), webhook signing and event types, or the embed snippet, plus the product's own
   docs, README quickstart and any `examples/` directory. Record the FRAMEWORKS.md facts, each
   with the `path:line` that proves it. Add every existing starter or example directory to
   `earlier`.

3. **Decide with FRAMEWORKS.md.** Run `choose_starter` unchanged with the facts, `earlier`
   and the `framework` input. On `no_change`, change no files and go to step 7.

4. **Pick one capability.** Use the `use_case` input when given, if the repository supports it;
   otherwise the Feature map's clearest capability that one call, or one short sequence of
   calls, can show. For webhooks it is receiving and verifying one event. Name each call the
   starter makes and the `path:line` that defines its name, arguments and response. A call you
   did not read is not allowed in the starter; say what is missing instead.

5. **Write the starter.** Create exactly the files `choose_starter` lists, under its path, and
   nothing outside it. Follow the repository's own examples when it has them.
   - Depend on the product's published package at the version its manifest declares, or call
     the HTTP API with the platform's `fetch` or `httpx`. At most two other dependencies beyond
     the framework and its type packages.
   - Secrets live in server code only: a Next.js route handler, the FastAPI app, the Express
     server. The page calls that server route. Only a key the repository documents as
     publishable may carry a public prefix. `.env.example` lists every variable with an empty
     value or `<placeholder>` and one comment each.
   - The page is one input, one button and the formatted result or a clear error. Handle the
     product's error responses; never print a stack trace or a key.
   - The README, in the founder's voice: one sentence on what it shows, prerequisites
     (runtime version, where to get a key, from the product's own docs URL in the repository),
     then clone, `cp .env.example .env.local` (or `.env` for FastAPI and Express), install, and
     the run command. Add a Vercel deploy button for Next.js only, with the repository URL and
     the starter's `root-directory`. Link the product's docs for the next step.

6. **Check.** Run `check_starter` from CHECK.md unchanged and fix every problem it names in the
   starter. Then run what this sandbox can run without installing anything: `git diff --check`,
   `python3 -m py_compile` on Python files, `node --check` on plain JavaScript, and a JSON parse of
   each manifest. Record each check not run and why, with the exact commands the founder runs
   instead. Never claim the starter was installed or started.

7. **Report.** Return a concise title (`Add a <framework> starter`, or `No change: <reason>`)
   and a body with RESULT.md's headings in order, and write the same content as the receipt.
   Never merge the pull request, deploy, publish a package, create a repository or contact
   anyone.
