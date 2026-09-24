# Scoring

`score = reach × gap × self_fixable`. Every factor is an integer, so two runs over the same
repository rank the same way. Show all three factors and the product in the inventory table.

## reach: how many users hit it

| value | when the message fires |
|---|---|
| 3 | install, setup, auth, first run, or the first call in the quickstart |
| 2 | the product's core, repeated action |
| 1 | an edge case, a rarely used flag, or an admin-only path |

Decide reach from where the raising code sits in the flow you read, such as the entrypoint,
the `init` command or the login handler. Do not decide it from how alarming the text sounds.
If the README quickstart walks through the code path, reach is 3.

## gap: how unanswered it is

| value | coverage | demand check |
|---|---|---|
| 4 | `uncovered` | `third_party` or `competitor` |
| 3 | `uncovered` | `none` or `not_checked` |
| 2 | `mentioned` | any |
| 1 | `documented` | any |
| 0 | `linked` | any |

`not_checked` scores like `none`, so an unchecked row can be under-ranked but never
over-ranked. Mark its gap with `*` in the table, for example `3*`.

A `gap` of 0 means the job is already done for that message. It stays in the table and is never
drafted.

## self_fixable: can a page actually help

| value | meaning |
|---|---|
| 2 | the user can resolve it with information a page can give: a config value, a flag, a version, a permission, or a file, input or setup step they control |
| 1 | the user can only work around it, or must contact support, or the cause is a product bug |

A self_fixable 1 message with high reach is still worth a page, because it tells users the bug
is known. Say so in the draft instead of inventing a fix.

## Example

`Error: DATABASE_URL is not set` fires in `cli/init.py:41` during `init`, appears nowhere in
`docs/`, and the search finds a Stack Overflow thread. reach 3 × gap 4 × self_fixable 2 = 24.
