# Checking the starter before the pull request

Run `check_starter` unchanged on the files you wrote, with the `choose_starter` result, the
`auth` fact and the product calls the starter makes. Deliver only when it returns an empty list.
Fix each problem it names in the starter; never edit the check or drop a call to pass it. If
you cannot fix a problem, change no files and return a no-change result naming it.

`files` maps each path, relative to the repository root, to its text. `calls` lists every call
the starter makes to the product (an SDK method, an HTTP endpoint, a webhook event or an embed
attribute), each with the repository `path:line` you read that defines it:
`{"call": "client.search.query", "evidence": "sdk/src/search.ts:42"}`.

```python
import re

PUBLIC_PREFIX = {"nextjs": "NEXT_PUBLIC_", "vite-react": "VITE_", "fastapi": None, "express": None}
SECRET_NAME = re.compile(r"SECRET|PRIVATE|TOKEN|PASSWORD|API_KEY|(?<!PUBLISHABLE_)KEY$")
SECRET_VALUE = re.compile(
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[abpr]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
EVIDENCE = re.compile(r"^[^\s:]+:\d+$")
MAX_FILES = 10


def _client_files(framework, relative, text):
    if framework == "nextjs":
        return text.lstrip().startswith(("'use client'", '"use client"'))
    if framework == "vite-react":
        return relative.startswith("src/") or relative == "index.html"
    if framework == "fastapi":
        return relative.startswith("static/")
    return False


def check_starter(choice, auth, files, calls):
    if not isinstance(choice, dict) or choice.get("outcome") != "patch":
        raise ValueError("check_starter needs a patch result from choose_starter")
    framework, root = choice["framework"], choice["path"]
    if framework not in PUBLIC_PREFIX:
        raise ValueError("unknown framework")
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise ValueError("files must map paths to text")
    if not isinstance(calls, list):
        raise ValueError("calls must be a list")
    problems = []

    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files; the pull request holds at most {MAX_FILES}")
    relative = {}
    for path in files:
        if not path.startswith(root + "/") or ".." in path.split("/"):
            problems.append(f"{path} is outside {root}/")
        else:
            relative[path[len(root) + 1 :]] = files[path]
    for name in choice["files"]:
        if name not in relative:
            problems.append(f"missing {root}/{name}")

    for path, text in files.items():
        if SECRET_VALUE.search(text):
            problems.append(f"{path} contains what looks like a real credential")

    secrets, public = [], []
    for number, line in enumerate(relative.get(".env.example", "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            problems.append(f".env.example line {number} is not NAME=value")
            continue
        name, value = match.groups()
        prefix = PUBLIC_PREFIX[framework]
        if prefix and name.startswith(prefix):
            public.append(name)
            if SECRET_NAME.search(name[len(prefix) :]):
                problems.append(f"{name} would ship a secret to the browser")
        elif SECRET_NAME.search(name):
            secrets.append(name)
            if value.strip() and not value.strip().startswith("<"):
                problems.append(f"{name} has a value in .env.example; leave it empty")
    if auth in ("secret_key", "oauth") and not secrets:
        problems.append(".env.example declares no server-side secret for a secret_key product")
    for name, text in relative.items():
        if _client_files(framework, name, text):
            for secret in secrets:
                if secret in text:
                    problems.append(f"{root}/{name} runs in the browser and reads {secret}")

    readme = relative.get("README.md", "")
    for needed in (".env.example", choice["run"]):
        if needed not in readme:
            problems.append(f"README.md does not mention {needed}")

    if not calls:
        problems.append("no product call is listed; a starter must call the product")
    for call in calls:
        if (
            not isinstance(call, dict)
            or not str(call.get("call", "")).strip()
            or not EVIDENCE.match(str(call.get("evidence", "")))
        ):
            problems.append(f"call without repository path:line evidence: {call!r}")
        elif not any(
            token in text
            for token in {call["call"].split()[-1], call["call"].split()[-1].rsplit(".", 1)[-1]}
            for text in relative.values()
        ):
            problems.append(f"{call['call']} is listed but no starter file uses it")
    return problems
```
