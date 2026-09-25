# Document contracts

The primary proposal is `brand/proposals/{run_id}/BRAND.md` (48,000 bytes maximum); its required
companion is `brand/proposals/{run_id}/DESIGN.md` (64,000 bytes maximum). Both are UTF-8 Markdown.
Use the exact paths resolved in context. Never truncate an existing document to fit a limit.

New BRAND.md contains one title followed by exactly these second-level headings:

1. `## Brand direction` — short audience, personality, impression and high-level voice.
2. `## Visual style` — colors and roles, hierarchy, type families/fallbacks, composition,
   imagery vocabulary and useful references. Label each palette value observed,
   founder-directed or proposed. Include every token palette hex value in this section.
3. `## Generation rules` — protected choices, permitted variation, supporting guidance and
   exclusions. Explain what downstream marketing should actually do.
4. `## Assessment and sources` — concrete findings, coverage, gaps, capture time and references.

Place one `json` fenced assessment block in Assessment and sources. The following is a shape
example, not a diagnosis or a default identity:

```json
{
  "schema": "tin-brand-assessment.v1",
  "identity_class": "distinctive_inconsistent",
  "evidence_status": "partial",
  "source_ids": ["site-home"],
  "captured_at": "2026-09-24T12:00:00Z",
  "findings": [{
    "observation": "The same palette appears with different heading hierarchies.",
    "source_ids": ["site-home"],
    "generation_implication": "Preserve the palette; use one heading hierarchy in new assets."
  }],
  "method": {"kind": "visual_capture", "rubric_version": "marketing-brand.v1"}
}
```

Allowed identity classes: distinctive_strong, distinctive_inconsistent, competent_generic,
weak, unknown. Evidence status: adequate, partial, insufficient, conflicting. One to five
findings, each with observation, source_ids and generation_implication. The block is at most
8,000 bytes. Do not add probabilities or other fields. A packet-only capture must say it used
supplied observations; method describes the rubric, not proof of independent browser coverage.

Declare every source ID with a Markdown reference definition, for example:
`[site-home]: https://example.com/ "Homepage, observed at actual capture time"`.
Use pinned GitHub commit/file links for source-code facts, and
`code.storage://projects/{project_id}@{project_revision}/{path}` for supplied project evidence.
Use actual times, revisions and observations. No credential-bearing links or private local paths.

End BRAND.md with one `json` fenced token block, at most 2,000 bytes. Required fields:

```json
{
  "schema": "tin-brand.v1",
  "name": "Example",
  "light": {"ink": "#182B24", "paper": "#FAF8F0", "accent": "#287A55"}
}
```

These are illustrative colors only. Extract or justify the project's actual values.
Optional top-level fields: direction (text), dark (a complete ink/paper/accent palette),
type (display/body/mono family strings), shape (sharp/soft/round). A palette may additionally
include signal; consumers can otherwise use accent. Every color is six-digit hexadecimal.
Do not invent a dark palette from an unseen theme. Do not add unknown fields, duplicate keys,
comments or a second block. Naming a font does not provide its files or trigger a download.

New DESIGN.md uses one title and exactly these second-level headings:

1. `## Visual foundations`
2. `## Components and patterns`
3. `## Screens and flows`
4. `## Constraints and evidence`

Describe useful knowns and specific gaps in each. Put coverage, source references, contradictions
and explicitly labeled recommendations in the last section. At least one attributed source is
required. No second palette token contract, assessments or machine-readable manifest is needed.
Existing compatible member documents are carried forward unchanged and need not use these new
section layouts. Existing BRAND.md must retain a valid tin-brand.v1 block.
