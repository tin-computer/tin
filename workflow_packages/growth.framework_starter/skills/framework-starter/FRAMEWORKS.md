# Choosing the starter

Record the facts below from the repository, each with the file that proves it, then run
`choose_starter` unchanged. It decides whether there is a starter to write, which framework it
uses and where it goes. Do not pick a framework by taste.

## Facts

- `surface`: the first thing an outside developer calls, from the repository itself.
  - `sdk`: a client library this repository publishes (a `package.json` with a name, version and
    exports that is not `"private": true`, or a `pyproject.toml` `[project]` that builds a wheel).
  - `http_api`: a documented public HTTP API with key or token auth, and no published client.
  - `webhooks`: the product calls the developer's server (signed webhook events).
  - `embed`: a script tag, web component or widget a developer drops into a page.
  - `none`: nothing an outside developer integrates. A dashboard-only SaaS, a marketing site,
    or an API that only the product's own frontend calls is `none`.
- `auth`: what the demo needs to call it: `secret_key` (server-side API key or token),
  `publishable_key` (a key meant to ship in the browser), `oauth`, or `none`.
- `languages`: the languages of the clients this repository publishes, from `typescript` and
  `python`. JavaScript counts as `typescript`. Empty for a raw HTTP API.
- `examples_dir`: the repository's existing examples directory (`examples`, `demos`,
  `packages/examples`) when there is one, otherwise empty.

`earlier` lists every framework that already has a starter: from earlier receipts at
`reports/framework-starter/*/RESULT.md` in the project state, from open pull requests, and from
directories already in the repository such as `examples/with-nextjs`. Each entry is
`{"framework": ..., "path": ...}`.

`requested` is the `framework` input, or empty.

## Frameworks

| id | when it fits | run command |
| --- | --- | --- |
| `nextjs` | TypeScript clients, HTTP APIs, OAuth; the key stays in a route handler | `npm run dev` |
| `fastapi` | Python clients, HTTP APIs, webhooks for Python teams | `uvicorn main:app --reload` |
| `vite-react` | embeds and publishable keys only; there is no server to hold a secret | `npm run dev` |
| `express` | webhooks and server-to-server calls from Node | `npm run dev` |

Every file in `FILES` is required, relative to the starter directory. The layout follows each
framework's own conventions: Next.js App Router, a single-module FastAPI app with one static
page, a Vite React app, and a TypeScript Express server.

```python
FRAMEWORKS = ("nextjs", "fastapi", "vite-react", "express")
SURFACES = ("sdk", "http_api", "webhooks", "embed", "none")
AUTH = ("secret_key", "publishable_key", "oauth", "none")
LANGUAGES = ("typescript", "python")

FILES = {
    "nextjs": (
        "package.json",
        "tsconfig.json",
        ".env.example",
        ".gitignore",
        "README.md",
        "app/layout.tsx",
        "app/page.tsx",
        "app/api/demo/route.ts",
    ),
    "fastapi": (
        "pyproject.toml",
        ".env.example",
        ".gitignore",
        "README.md",
        "main.py",
        "static/index.html",
    ),
    "vite-react": (
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
        "index.html",
        ".env.example",
        ".gitignore",
        "README.md",
        "src/main.tsx",
        "src/App.tsx",
    ),
    "express": (
        "package.json",
        "tsconfig.json",
        ".env.example",
        ".gitignore",
        "README.md",
        "src/index.ts",
    ),
}
RUN = {
    "nextjs": "npm run dev",
    "fastapi": "uvicorn main:app",
    "vite-react": "npm run dev",
    "express": "npm run dev",
}
LANGUAGE_FRAMEWORKS = {"typescript": ("nextjs", "express"), "python": ("fastapi",)}
SURFACE_FRAMEWORKS = {
    "http_api": ("nextjs", "fastapi", "express"),
    "webhooks": ("express", "nextjs", "fastapi"),
    "embed": ("vite-react", "nextjs"),
}
BROWSER_ONLY = ("vite-react",)
MAX_FILES = 10


def _no_change(reason):
    return {
        "outcome": "no_change",
        "framework": None,
        "path": None,
        "files": (),
        "run": None,
        "reason": reason,
    }


def _examples_dir(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("examples_dir must be a short relative path or empty")
    value = value.strip().strip("/")
    if not value:
        return "examples"
    if value.startswith(".") or "\\" in value or any(p in ("", "..") for p in value.split("/")):
        raise ValueError("examples_dir must stay inside the repository")
    return value


def choose_starter(facts, earlier=(), requested="", max_files=MAX_FILES):
    if not isinstance(facts, dict) or set(facts) != {
        "surface",
        "auth",
        "languages",
        "examples_dir",
    }:
        raise ValueError("facts must name surface, auth, languages and examples_dir")
    surface, auth, languages = facts["surface"], facts["auth"], facts["languages"]
    if surface not in SURFACES or auth not in AUTH:
        raise ValueError("unknown surface or auth")
    if not isinstance(languages, list) or any(lang not in LANGUAGES for lang in languages):
        raise ValueError("languages must be a list drawn from LANGUAGES")
    if requested not in ("", *FRAMEWORKS):
        raise ValueError("requested must be empty or a supported framework")
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise ValueError("max_files must be an integer from 1 to 10")
    if not isinstance(earlier, (list, tuple)) or len(earlier) > 50:
        raise ValueError("earlier must be a short list")
    done = {}
    for item in earlier:
        if (
            not isinstance(item, dict)
            or item.get("framework") not in FRAMEWORKS
            or not isinstance(item.get("path"), str)
            or not item["path"].strip()
        ):
            raise ValueError("each earlier starter needs a supported framework and its path")
        done.setdefault(item["framework"], item["path"])
    base = _examples_dir(facts["examples_dir"])

    if surface == "none":
        return _no_change(
            "nothing a developer integrates: no published client, public API, webhook or embed "
            "in this repository"
        )
    if surface == "sdk":
        if not languages:
            raise ValueError("an sdk surface needs the language it is published in")
        ordered = [
            fw for lang in LANGUAGES if lang in languages for fw in LANGUAGE_FRAMEWORKS[lang]
        ]
    else:
        preferred = {fw for lang in languages for fw in LANGUAGE_FRAMEWORKS[lang]}
        options = SURFACE_FRAMEWORKS[surface]
        ordered = [fw for fw in options if fw in preferred] + [
            fw for fw in options if fw not in preferred
        ]
    if auth in ("secret_key", "oauth"):
        ordered = [fw for fw in ordered if fw not in BROWSER_ONLY]
    ordered = [fw for fw in ordered if len(FILES[fw]) <= max_files]

    if requested:
        if requested not in ordered:
            return _no_change(
                f"{requested} cannot demonstrate this {surface} safely; "
                f"fits: {', '.join(ordered) or 'none'}"
            )
        if requested in done:
            return _no_change(f"already has a {requested} starter at {done[requested]}")
        ordered = [requested]
    remaining = [fw for fw in ordered if fw not in done]
    if not remaining:
        return _no_change(
            "every framework that fits already has a starter: "
            + ", ".join(f"{fw} at {done[fw]}" for fw in ordered)
        )
    framework = remaining[0]
    return {
        "outcome": "patch",
        "framework": framework,
        "path": f"{base}/{framework}-starter",
        "files": FILES[framework],
        "run": RUN[framework],
        "reason": "",
    }
```
