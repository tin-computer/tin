# Report Layout: Starter Repository Specification

The workflow writes a clean, actionable Markdown artifact to the declared output path.
Follow this exact layout. All sections must be present.

````markdown
# Framework Starter: <Product Name> + <Framework Name>

Status: complete | diagnostic | unsupported
Date: <YYYY-MM-DD>
Framework: <Framework ID>
Use case: <Demonstrated capability>
Context: <Code map, Feature map, growth plan, style guide: "read" or "none found" for each>

<One-sentence executive summary stating the starter concept, the developer audience it captures, and where to publish it.>

---

## 1. Concept and Target Audience

- **Target developer**: <Persona, e.g. Full-stack Next.js developer building AI apps>
- **The problem solved**: <What pain point this starter removes in under 5 minutes>
- **Why this framework**: <Evidence from Code map / ecosystem affinity>
- **Target gallery / showcase**: <e.g. Vercel Templates Gallery / GitHub showcase>

---

## 2. Repository Blueprint

- **Suggested repository name**: `<starter_name>`
- **Repository description**: `<One-line GitHub repository description (max 120 chars)>`
- **Key topics/tags**: `<tag1>, <tag2>, <tag3>, <tag4>`
- **File tree**:
```text
<directory tree showing all files in the starter>
```

---

## 3. Environment & Configuration

### `.env.example`
```env
<clean environment variable declarations with explanatory comments>
```

### Dependency Manifest (`package.json` or `pyproject.toml`)
```json
<minimal dependencies needed for the starter>
```

---

## 4. Implementation Code

### Core Integration Module (`app/api/...` or `main.py`)
```typescript
<minimal, robust, runnable server-side route or handler calling the product>
```

### Minimal Client UI (`app/page.tsx` or `src/App.tsx`)
```tsx
<simple, clean UI allowing the developer to test the integration immediately>
```

---

## 5. Starter README

```markdown
<complete 5-minute quickstart README for the starter repo, including clone, env setup, run command, and deploy button>
```

---

## 6. Template Gallery Submission Packet

- **Gallery name**: <e.g. Vercel Template Marketplace>
- **Submission URL / process**: <URL or instructions for submitting community templates>
- **Listing title**: <Listing title>
- **Short description**: <Catchy 1-2 sentence description>
- **Live demo URL recommendation**: <e.g. deployed on Vercel/Railway>
- **Required preview assets**: <Recommended screenshot dimensions and mock views>

---

## 7. Verification & Grounding Check

- **Code map integration**: <path:line citation in product repo confirming API/SDK existence>
- **Feature map capability**: <section citation confirming the feature works as described>
- **Security review**:
  - [x] No private secrets or master keys exposed to client bundles
  - [x] Environment variable placeholders clearly documented
  - [x] Error handling on failed API calls
- **Run command**: `<exact terminal command to run locally>`

```tin-starter-state
{
  "version": 1,
  "product": "<product_name>",
  "framework": "<framework_id>",
  "repo_name": "<starter_name>",
  "verified_at": "<YYYY-MM-DD>"
}
```
````

### Diagnostic Output (When Code map is missing)

If `wiki/INDEX.md` lacks `### Code map`:

```markdown
# Framework Starter

Status: diagnostic

No `### Code map` was found in `wiki/INDEX.md`. Run "Map the product from its code" (`product.code_map`) first to inventory supported languages, SDKs, and endpoints, then run this workflow again.
```
