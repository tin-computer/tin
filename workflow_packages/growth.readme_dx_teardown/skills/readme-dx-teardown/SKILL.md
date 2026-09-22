---
name: readme-dx-teardown
description: Audit a GitHub repository's README and quickstart documentation for developer onboarding friction and actionable DX improvements.
---

# README & Developer Experience (DX) Teardown

## Purpose
Inspect an open-source repository's `README.md` and related onboarding docs to identify friction bottlenecks preventing developers from reaching "Time-to-First-Hello-World" quickly. Produce a structured, actionable teardown report (`reports/README_DX_TEARDOWN.md`) that a founder or devrel engineer can use for developer acquisition and community growth.

## Audit Checklist
1. **Value Proposition & Hook**: Is it instantly clear what problem the project solves within 5 seconds of scanning the header?
2. **Time-to-First-Hello-World**: Can a developer run a minimal working example in under 60 seconds? Are all copy-paste commands explicit and working?
3. **Prerequisites & Dependencies**: Are required runtime versions (Node/Python/Go/Rust/Docker/OS constraints) clearly declared up front before terminal commands fail?
4. **Architecture & Visual Clarity**: Is there a visual flow, system diagram, or concise overview of key concepts and folder structures?
5. **Troubleshooting & Friction Points**: What are the top 3 friction points where developers get stuck or drop off during initial setup?

## Report Structure
Write `reports/README_DX_TEARDOWN.md` formatted as follows:
- **Repository Overview**: Target repo, primary language/stack, target developer audience.
- **Executive DX Scorecard**: Time-to-First-Hello-World rating, Quickstart clarity, Prerequisites score.
- **Identified Onboarding Friction Bottlenecks**: Concrete code snippets/lines where developers encounter friction or missing details.
- **Proposed Quickstart & README Refinements**: Copyable, rewritten README sections, badge recommendations, and visual layout suggestions.
- **Growth & Conversion Impact**: How these DX fixes improve contributor conversion, star-to-use retention, and developer acquisition.
