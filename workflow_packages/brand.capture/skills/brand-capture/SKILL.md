---
name: brand-capture
description: Inspect a bounded source sample and write one reviewable brand/design pair.
---

# Capture brand and design

Read CONTRACT.md before writing. Keep this short and useful to the next marketing workflow,
not a general brand strategy exercise. The durable outputs are exactly two Markdown files.

## 1. Read the prepared context

Use the trusted `brand_capture` object. Its project revision pins both existing destinations,
reference packet and source inputs. Read the current documents, selected packet, relevant
wiki/INDEX.md and `.agents/skills/writing-style/SKILL.md` in the project-state checkout.
Do not treat the source repository as project state. Do not expose local absolute paths.

An existing compatible BRAND.md or DESIGN.md must be copied byte-for-byte to its proposal.
Do not fix wording, headings, palettes or typography while carrying it forward. This is first
capture, not refresh. Use existing brand guidance as authority even when observed site colors
are different. Document that difference in a newly generated DESIGN.md.

Authority: existing member-authored guidance and explicit founder choices, then identified
source materials, then observed implementation, then clearly labeled conservative proposals.
Detailed writing-style guidance remains authoritative for prose. Current brief determines
subject and message; a classifier cannot grant permission to replace identity.

## 2. Inspect once

When a URL exists, inspect the homepage and one representative deeper product, feature or
documentation page with the available browser. Include relevant desktop and mobile views,
computed CSS colors and font declarations. Use `camoufox.set_viewport(1440, 900)` and
`camoufox.set_viewport(390, 844)`, verify the returned dimensions, and inspect
`camoufox.screenshot()` at both sizes. Scroll and capture a representative lower section when
needed. A narrow Firefox viewport proves responsive layout, not mobile Safari or touch behavior.
Never substitute `window.resizeTo`, changed CSS, or responsive stylesheet rules for a rendered
narrow viewport. A single-page site, blocked page, or sufficient
supplied documentation can justify narrower coverage; state the exception. A CSS declaration
does not prove a font loaded. Camoufox masks `document.fonts` / `FontFace.status` and can report
`error` for a downloaded, rendered font. Never call that alone a font-loading failure. Check
font resource transfers in `performance.getEntriesByType('resource')`, `network_failures`,
console diagnostics and the screenshot. If necessary, compare the declared family's rendered
text widths with its fallback using temporary measurement elements, then remove them. Record
download evidence and rendering evidence separately; neither a successful download nor a
computed family name alone proves which font painted every glyph. A failed font request or
decoder error is a real limitation; ambiguous evidence stays unverified. Do not change the
site's fonts or the browser's fingerprint mask to force a positive result.
Do not infer application screens, flows or states you did not see.

When a source snapshot is supplied, inspect relevant styles, tokens, components, routes and
design documentation without installs, builds or source modifications. Cite repository facts
by exact commit and path from `workspace`; distinguish code declarations from deployed browser
observations. Record snapshot coverage and omissions. A filtered archive is not a full clone.
When evidence conflicts, record the disagreement. Never silently call screenshot evidence
code-backed, or a caller's packet your own independent observation.

Use at most three supplied visual references. Note what each contributes: color relationship,
line quality, framing or materials. Available original assets, external references, unavailable
assets and prose descriptions are distinct. Never pretend a reconstructed logo is the original.
Use only allowed public browsing; do not submit forms, sign in or modify the site.

## 3. Assess ingredients, then give useful generation rules

Assess specificity, coherence, craft and context fit separately. A common font, gradient,
conventional layout or unusual aesthetic is not independently a defect. Do not assume an OSS
project has poor branding. One palette can work while hierarchy or composition is inconsistent.

Store the identity label only in the assessment JSON. Human findings describe observable
facts and their consequences. Use these strategies:

- distinctive_strong: follow the identity closely.
- distinctive_inconsistent: preserve recognizable ingredients, make their application consistent.
- competent_generic: retain identity; make imagery and message specific to the actual product.
- weak: keep protected anchors, add only necessary supporting rules for new outputs.
- unknown: preserve known facts, describe gaps, propose defaults only where needed and permitted.

Evidence sufficiency and founder openness are separate from quality. Never invent attachment,
a probability, or a confidence score. In capture intent, record the existing identity; necessary
supporting defaults must be explicitly proposed for approval. In develop intent, modest
supporting changes are permitted within the notes. Neither is permission for a full rebrand.

“Keep our blue” preserves its value, not merely a vague blue family. “Keep our font” preserves
family and role. Prefer changes to supporting spacing, hierarchy, accent frequency, crop or
imagery specificity. Never normalize everyone into Tin's palette, fonts or illustration style.
State constraints concretely in Generation rules. Low contrast can be documented and handled
with a proposed supporting role; do not silently change a protected value.

## 4. Write both documents

BRAND.md is normative guidance for future marketing; DESIGN.md is evidence about existing
product design. Follow the four sections in CONTRACT.md. Include compact imagery language:
medium, texture/material, light, perspective, crop and density where supported. Give one reusable
art-direction sentence when useful. References need a concrete contribution, not just a brand
name. Avoid empty adjectives such as “premium” without instructions an image model can use.

Design coverage: visible foundations, components, page roles, information architecture, flows
and responsiveness actually observed, accessibility observations, unavailable states and source
limitations. Separate recommendations from observations. Unknown information is better than
invented component names, token scales, font certainty or compliance claims. A homepage and
feature page support public-site documentation, not an unseen authenticated application.

Reuse one evidence set. Cite each factual observation to its source ID and give Markdown
reference definitions. Keep capture time and specific gaps. Shared source IDs identify the same
source in both new documents; use distinct IDs for different revisions or perspectives.
Do not force observed design colors to equal a different founder-directed brand palette.

## 5. Check before finishing

Both proposal paths must be in project state. Compare any carried-forward document with its
active source using a byte comparison. For new documents, check all four headings, source IDs,
JSON syntax (including duplicate keys), palette/prose consistency, byte limits and complete
required sections. Both files must be readable after the sandbox ends. No extra durable files.

Stop with a clear source limitation if nothing usable can be inspected. Do not fill a plausible
brand from the domain name alone. The founder reviews the pair; the trusted service applies
it later. Writing the proposals never authorizes their use as active guidance.
