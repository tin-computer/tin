# directories.submission_pack

Turns your product metadata into a ready-to-use directory submission pack in under
10 seconds. One Markdown section per directory, trimmed to that site's exact character
limits, with a tracker table at the end.

## What it does

Every SaaS founder eventually spends 3–4 hours filling out the same fields — name,
tagline, description, tags — on 10–20 directory submission forms. Each site has slightly
different character limits, different tag counts, different required fields. The work is
too tedious to do carefully and too important to skip (directory listings drive backlinks,
organic discovery, and first-week signups).

This workflow turns that into a 5-minute review job:

1. You supply your product name, tagline, URL, and optional description and tags once.
2. The workflow reads its directory fixture (10 sites with real field specs).
3. For each directory, it trims each field to that site's limit at a word boundary,
   flags every truncation with a warning, and validates that all fields are within spec.
4. It writes a single `reports/DIRECTORY_SUBMISSION_PACK.md` to your project Files.
5. You review, copy-paste into each form, and tick off the tracker table.

It does **not** auto-submit to any directory. Directories require human accounts, 2FA,
and often manual review. Auto-submission would violate their terms of service.

## Inputs

| Field | Required | Notes |
|-------|----------|-------|
| `product_name` | Yes | ≤ 60 chars. Canonical name — consistent everywhere. |
| `tagline` | Yes | 10–120 chars. Will be trimmed per directory. |
| `website_url` | Yes | Must start with `https://`. |
| `short_description` | No | ≤ 300 chars. Used for directories with longer fields. |
| `pricing_summary` | No | ≤ 200 chars. e.g. "Free up to 1k events/day, paid from $19/mo" |
| `category_tags` | No | Up to 10 tags. Each trimmed per directory limit. |
| `founder_email` | No | Required by G2 and Capterra. Include if submitting to those. |
| `target_directories` | No | Leave empty for all 10. Or specify: `["Product Hunt", "G2"]`. |

## Supported directories (10 in fixtures)

| Directory | DA | Notes |
|-----------|----|-------|
| Product Hunt | 91 | Best Tue/Wed 00:01 PST; 5+ days account age |
| G2 | 92 | Needs business email; 2–3 week review |
| Capterra | 90 | Needs business email; 5–7 day review |
| AlternativeTo | 85 | Free, fast, evergreen |
| BetaList | 71 | Pre-launch focus; free queue or $129 to skip |
| Indie Hackers | 79 | Best for bootstrapped/revenue-transparent products |
| SaaSHub | 73 | Often auto-indexes; manual submission = faster |
| Peerlist | 57 | Dev tools, APIs, B2D |
| Uneed | 52 | Daily curated launches; Mon–Wed best |
| Startup Stash | 66 | Curated resource directory; 1–2 week review |

## Output

`reports/DIRECTORY_SUBMISSION_PACK.md`

Structure:
- Header with product name, URL, and validation summary
- One `##` section per directory with:
  - Submission URL and Domain Authority
  - Reviewer notes (timing, requirements)
  - Formatted fields ready to copy-paste
  - Inline warnings for trimmed fields
- Submission tracker table (Status column for you to fill in)

## How to run

In the Tin dashboard, select **Directory submission pack** and fill in the form.
Or via MCP in your coding agent:

```
start_workflow(
    workflow_id="<uuid from registry>",
    inputs={
        "project_id": "<your project id>",
        "product_name": "Your Product",
        "tagline": "One line that sells it",
        "website_url": "https://yourproduct.com",
        "pricing_summary": "Free plan, paid from $19/mo",
        "category_tags": ["SaaS", "Productivity", "AI"]
    }
)
```

## Known limitations

- **10 directories in fixture.** Adding a new directory requires editing
  `fixtures/directories.json` — no network discovery. Character limits in the fixture
  reflect the state of each site at time of authoring and may drift.
- **No auto-submission.** By design. Directories require login, 2FA, and human review.
- **No image asset handling.** Logos and screenshots are referenced in the tracker notes
  but not generated or validated by this workflow.
- **Description fallback.** If `short_description` is empty, the description field for
  directories with large limits will contain only the trimmed tagline. A longer
  `short_description` produces much better results for G2 and Capterra.
- **Email required for G2/Capterra.** If `founder_email` is omitted, those directories
  will include a warning in their section.
