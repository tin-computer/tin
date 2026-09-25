# Framework ecosystems reference

This reference documents the supported framework ecosystems for starter repositories.
Each entry describes the ecosystem signals in code, official template requirements,
recommended project layout, and the primary template directory or gallery to submit to.

---

## nextjs — Next.js (React / Full-stack)

- **Ecosystem signals in Code map**:
  - TypeScript/JavaScript SDK, REST/JSON API, OAuth flows, server-side secrets or API keys.
  - Webhooks or serverless handlers that benefit from Route Handlers (`app/api/.../route.ts`).
  - React components, widgets, or embeddable UI elements.
- **Gallery / Showcase**:
  - Vercel Templates Gallery: `https://vercel.com/templates`
  - GitHub topics: `nextjs-starter`, `nextjs-template`.
- **Target audience**: Full-stack web developers, SaaS builders, AI app creators.
- **Minimal required files**:
  - `package.json` (next, react, react-dom, typescript, devDependencies)
  - `tsconfig.json`
  - `.env.example` (clean variable names, placeholders, comments)
  - `app/layout.tsx` (root HTML shell)
  - `app/page.tsx` (interactive UI demonstrating the integration)
  - `app/api/integration/route.ts` (server-side API route securely calling the product)
  - `README.md` (one-click deploy button, prerequisites, 3-step local setup)
- **Hard gates**:
  - Must not expose backend API keys or master credentials to the client/browser bundle.
  - Must run with `npm run dev` out of the box after copying `.env.example` to `.env.local`.

---

## fastapi — FastAPI (Python / Modern API)

- **Ecosystem signals in Code map**:
  - Python SDK, PyPI package, data science / AI / ML tooling, asynchronous workers.
  - Background task processing, webhook listeners, streaming responses.
- **Gallery / Showcase**:
  - FastAPI ecosystem repositories, GitHub topics: `fastapi-template`, `fastapi-starter`.
- **Target audience**: Python backend engineers, AI/ML engineers, data developers.
- **Minimal required files**:
  - `pyproject.toml` or `requirements.txt` (fastapi, uvicorn, pydantic-settings, product SDK/httpx)
  - `.env.example` (environment configuration template)
  - `main.py` (FastAPI app factory, lifespan or startup configuration)
  - `routers/demo.py` or single-file endpoint demonstrating the core API call
  - `README.md` (virtualenv setup, dependency installation, uvicorn run command)
- **Hard gates**:
  - Must use modern Pydantic v2 / typed models for request/response validation.
  - Must run with `uvicorn main:app --reload` after setting `.env`.

---

## vite-react — Vite + React (Client-side Single Page App)

- **Ecosystem signals in Code map**:
  - Client SDK, public API with CORS enabled, publishable keys (e.g. Clerk, Stripe publishable key, Firebase client config).
  - Embeddable canvas, dashboard widgets, client-side state management.
- **Gallery / Showcase**:
  - Vite templates, awesome-vite, GitHub topics: `vite-template`, `react-starter`.
- **Target audience**: Frontend engineers, single-page application developers, JAMstack developers.
- **Minimal required files**:
  - `package.json` (vite, react, react-dom, @vitejs/plugin-react, typescript)
  - `vite.config.ts`
  - `index.html`
  - `.env.example` (`VITE_` prefixed public keys)
  - `src/App.tsx` (interactive demo UI)
  - `src/main.tsx` (entry point)
  - `README.md` (quick start, build command, deploy instructions)
- **Hard gates**:
  - Only safe publishable/public keys may be used; never include private secrets in client builds.

---

## express — Node.js / Express (Backend Microservice)

- **Ecosystem signals in Code map**:
  - Node.js SDK, server-to-server webhooks, enterprise middleware, microservices.
- **Gallery / Showcase**:
  - GitHub topics: `express-starter`, `nodejs-template`.
- **Target audience**: Node.js backend engineers, traditional microservice architects.
- **Minimal required files**:
  - `package.json` (express, dotenv, typescript, ts-node-dev)
  - `tsconfig.json`
  - `.env.example` (port, secret keys)
  - `src/index.ts` (Express app listening on PORT)
  - `src/routes/webhook.ts` or integration endpoint
  - `README.md` (prerequisites, setup, npm run dev)
- **Hard gates**:
  - Must handle error cases gracefully without leaking internal stack traces.
