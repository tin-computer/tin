---
name: framework-starter
description: Turn product APIs, SDKs, and features from the Code map into a turnkey starter repository specification and gallery submission packet.
---

## Purpose

Developers adopt developer tools, APIs, and platforms during the "zero-to-one" moment of a new
project or hackathon prototype. Instead of reading documentation, they clone starter repositories
and build on top of working boilerplate.

This skill maps the product's actual capabilities from project memory, selects the highest-affinity
developer framework, and produces a complete, copy-paste starter repository specification with
code, environment variables, a quickstart README, and template gallery submission copy.

This workflow is research and drafting only: it never creates GitHub repositories, registers packages,
or executes external network calls.

---

## 1. Station 1: Load Project Context & Memory

1. Read `wiki/INDEX.md`. Find `### Code map`. If it is missing or contains no public APIs, SDKs,
   or surfaces, write the diagnostic report from REPORT.md and stop. Do not guess API shapes.
2. Read `### Feature map` from `wiki/INDEX.md` when present. Note the primary user-facing
   capabilities and what value the product delivers.
3. Read `reports/GROWTH_ONBOARDING_PLAN.md` when present. Check target buyer persona and any
   binding `hard no: ...` directives (e.g. `no unbacked claims`: every starter feature must exist
   in code; `no third-party dependencies`: keep starter dependencies minimal).
4. Read `.agents/skills/writing-style/SKILL.md` when present for tone and voice guidelines.
5. Record which files were found and read for the `Context:` header.

---

## 2. Station 2: Identify the Demonstrated Capability

Select ONE focused capability that proves the product's value in under 5 minutes:
- Prioritize: A core API call, authentication handshake, real-time sync, or webhook handler.
- If `use_case` input is provided, ground that use case in the product's actual endpoints.
- If `use_case` is omitted, choose the single clearest value proposition from the Feature map.
- Avoid multi-stage enterprise setups; the starter must be understandable by an individual developer.

---

## 3. Station 3: Select the Framework Ecosystem

Compare the product's language and surfaces against `FRAMEWORKS.md`:
- If `target_framework` input is specified, validate that it exists in `FRAMEWORKS.md`. If it is
  unsupported, set `Status: unsupported`, explain why, and recommend a supported alternative.
- If `target_framework` is empty, infer the best fit:
  - If TypeScript/JS SDK or REST endpoints exist: choose `nextjs` (broadest full-stack reach).
  - If Python SDK / async AI tooling: choose `fastapi`.
  - If client-only widgets / publishable keys: choose `vite-react`.
  - If backend webhooks / serverless: choose `express`.

---

## 4. Station 4: Determine the Minimum Integration Glue

Define the minimal contract required to demonstrate the capability:
- Credentials: identify needed environment variables (e.g. `PRODUCT_API_KEY`). Provide safe placeholders.
- Security boundary: enforce server-side execution for secret keys. Only publishable keys may appear
  in client components.
- Dependencies: select at most 2-3 essential packages beyond the framework itself.

---

## 5. Station 5: Construct the Starter Blueprint

Draft the file tree and component structure:
- Repository naming: `<starter_name>` input if provided, otherwise `<product-slug>-<framework>-starter`.
- Structure: follow standard framework conventions from `FRAMEWORKS.md` (e.g. Next.js App Router).

---

## 6. Station 6: Generate Implementation Code & Configuration

Draft clean, idiomatic, fully runnable code:
1. Configuration files: `package.json` / `pyproject.toml`, `.env.example`, `tsconfig.json`.
2. Server integration: server route or endpoint making the product call with typed responses
   and robust error handling.
3. Demo UI: minimal, clean client component allowing immediate verification (input field + submit button
   + formatted output or status banner).

---

## 7. Station 7: Generate the Quickstart README

Write a concise README following standard open-source starter conventions:
1. Title and 1-paragraph summary with value proposition.
2. Deploy button (e.g. Deploy with Vercel) link structure.
3. Prerequisites (Node.js/Python version, product account signup link).
4. Step 1: Clone and install dependencies.
5. Step 2: Configure environment variables (`cp .env.example .env.local`).
6. Step 3: Run development server (`npm run dev` or `uvicorn`).
7. Next steps: links to official documentation for advanced use cases.

---

## 8. Station 8: Draft Gallery Submission Packet

Prepare the submission copy for relevant template marketplaces (e.g. Vercel Marketplace,
GitHub Showcases, Awesome lists):
- Listing title (max 60 characters).
- Short description (max 160 characters).
- Long description / key features bullet points.
- Screenshot / preview recommendation (dimensions and key state to display).

---

## 9. Station 9: Validate & Publish Report

Verify all evidence before writing output:
- Ensure all API endpoints or methods referenced exist in the Code map.
- Check that no real secrets or credentials appear in the output.
- Write the final document following `REPORT.md` layout to the declared output path.
- Include the `tin-starter-state` block with run metadata.
