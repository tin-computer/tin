# Brand capture qualification

These are synthetic packet-only cases, not live websites or visual-quality acceptance.
Copy each `sources/*.md` to the matching `inputs/brand-<case>.md` path in a fresh authorized
evaluation project with neither active document present. Each case needs its own fresh
project because successful approval creates both active files. Leave GitHub disabled.

Packets are supplied observations, not instructions to browse their illustrative URLs.
The evaluator stops for human review and never approves. A maintainer must inspect both
proposals and the rubric; success after approval does not itself establish quality.
Cases intentionally cover an expressive identity, a restrained technical identity, and
inconsistent execution with a protected anchor. They must not converge on one house style.

Offline backend tests prove preservation, structure, source attribution requirements and
adoption. `check` validates these cases and the manifest without buying a model call:

```sh
uv run python -m tin_lite.workflow_qualification_cli check \
  workflow_packages/brand.capture/workflow.json
```

No model outputs, paid costs or live acceptance results are asserted here. Browser-based
capture, blocked/deeper-page coverage, source-code corroboration and actual branded diagrams
and character outputs need separately authorized acceptance runs.
