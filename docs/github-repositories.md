# GitHub repositories

A project links one GitHub connection, and that connection names one **website repository**,
plus up to five optional **code repositories**. A founder whose marketing site and app live
apart, for example `acme/www` and `acme/app`, picks `acme/www` as the website and adds
`acme/app` under "Other repositories". The dialog says that workflows read them and that a
workflow changing the app may open a pull request there for review, never a merge.

## Who writes where

| Repository | Read by | Written by |
| --- | --- | --- |
| Website (`selected_repository`) | Every procedure | `website.change`, content delivery, `content.refresh`, technical fixes, the `ads.launch` tag change, and any procedure without a code output. The merge rules in [website.change](website-change.md) apply only here. |
| Code (`code_repositories`) | Every procedure | Only procedures that declare `"repository": "code"` on their pull-request output (`growth.signup_source`, `growth.framework_starter`). Tin opens an unmerged pull request and never merges or commits there. |

Every site write keeps its existing checks (`github_repository_binding`,
`repository_serves_site`, protected paths, required checks) unchanged, because a pinned
binding always names the website repository.

## Choosing them

- The dashboard's GitHub dialog asks for the website repository as before. When the
  installation offers more than one, an optional checklist below it lists the others. Only
  repositories the connecting person can push to appear (`user_repositories`).
- `PUT /api/projects/{id}/integrations/infra.github/code-repositories` with
  `{"repositories": ["owner/name", ...]}` sets the list, and the MCP tool
  `set_code_repositories` does the same. Choosing a code repository as the website removes it
  from the list. Reconnecting GitHub clears both, as it always cleared the website choice.

## In a procedure run

- At preparation the run pins its repository list once (`{run_id}:procedure_repositories`).
  A repository added later does not change a run that has already started.
- The repository the run works in stays at `/home/user/project`. Every other connected
  repository is a read-only snapshot at `/home/user/repos/<owner>/<name>`. In a
  `project.state` run all of them are there, including the website.
- Snapshots use the same gateway and bounds as a workspace (`repository_limits.py`). All
  references in one run share one budget. A repository that can't be read, or doesn't fit,
  is listed as unavailable with the reason, and the run goes on without it.
- The brief gets a `CONNECTED REPOSITORIES` list naming each repository's role, path and
  whether this run may edit it. The same list is in the trusted run context.
- A definition can opt out with `"repositories": "none"` on its workspace. A run where the
  founder turned repository evidence off (an optional repository, as in `brand.capture`)
  gets no references either.
- A procedure with `"repository": "code"` on its output works in, and opens its pull request
  against, the code repository its `expected_repository` input names. Without that input it
  uses the first code repository, and without any code repository the website repository.
  The choice is pinned once per run (`{run_id}:procedure_target_repository`).

`workflow.code` packages don't receive repositories. They read the Code map in project memory,
which `product.code_map` writes from every connected repository.
