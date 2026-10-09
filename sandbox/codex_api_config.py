"""Trusted isolated-controller configuration; never contains the provider API key."""

import json
import os
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

WORKER_PROXY = Path("/home/user/.tin-lite/proxy-worker.json")


def studio_values(env):
    """Delegate only this run's bounded voice capability, never model/storage auth."""
    if env.get("TIN_PROCEDURE_STUDIO") != "1":
        return {}
    values = {key: env.get(key, "") for key in ("TIN_RUN_TOOLS_URL", "TIN_RUN_TOOLS_GRANT")}
    if not values["TIN_RUN_TOOLS_URL"].startswith("https://") or any(
        not value or "\x00" in value or "\n" in value for value in values.values()
    ):
        raise ValueError("Studio API requires its run-bound voice capability")
    return values


def command_proxy(env):
    """This run's forward-proxy grant, so commands can make public requests.

    The sandbox fence admits only Tin's own hosts directly; public pages go through the
    proxy, which refuses private addresses. The grant expires with the run.
    """
    url = env.get("HTTPS_PROXY", "")
    if not url:
        return {}
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username != "tin-run"
        or not parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or "\x00" in url
        or "\n" in url
    ):
        raise ValueError("Invalid run-bound proxy configuration")
    hosts = [*env.get("NO_PROXY", "").split(","), "127.0.0.1", "localhost", "::1"]
    bypass = ",".join(dict.fromkeys(host.strip() for host in hosts if host.strip()))
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "NO_PROXY": bypass,
        "no_proxy": bypass,
    }


def worker_shell_policy(env):
    """Codex constructs command environments; exec-server inheritance is insufficient."""
    values = {**studio_values(env), **command_proxy(env)}
    if not values:
        return ""
    return (
        '\n[shell_environment_policy]\ninherit = "none"\n'
        "[shell_environment_policy.set]\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
        + "\n"
    )


def write_worker_proxy(path, env):
    """Only the controller writes this; the root helper hands it to every author command."""
    values = command_proxy(env)
    path.unlink(missing_ok=True)
    if not values:
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(values, handle)


def configure(path, env):
    url = env.get("TIN_CODEX_API_URL", "")
    parsed = urlsplit(url)
    if (
        env.get("TIN_PROCEDURE_ISOLATED") != "1"
        or not env.get("TIN_CODEX_API_GRANT")
        or env.get("TIN_BROKER_GRANT")
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/internal/codex-api/")
        or not parsed.path.endswith("/v1")
    ):
        raise ValueError("Invalid run-bound Codex API configuration")
    existing = path.read_text()
    config = tomllib.loads(existing)
    if config.get("model") != "gpt-6-sol" or "model_provider" in config:
        raise ValueError("Codex API requires the pinned model and a fresh controller")
    if path.with_name("auth.json").exists():
        raise ValueError("Codex API controller must not contain ChatGPT credentials")
    contract = json.loads(env.get("TIN_CODEX_API_CONTRACT", "{}"))
    context = ""
    protocol = contract.get("protocol")
    contexts = {
        "tin-codex-api-v2": (128_000, 96_000),
        "tin-codex-api-v3": (128_000, 96_000),
        "tin-codex-api-v4": (1_050_000, 922_000),
        "tin-codex-api-v5": (256_000, 200_000),
    }
    if protocol in contexts:
        window, compact = contexts[protocol]
        if (contract.get("context_window"), contract.get("auto_compact_tokens")) != (
            window,
            compact,
        ):
            raise ValueError("Unknown Codex API context contract")
        if "model_context_window" in config or "model_auto_compact_token_limit" in config:
            raise ValueError("Codex API requires a fresh context configuration")
        context = f"model_context_window = {window}\nmodel_auto_compact_token_limit = {compact}\n"
    elif protocol not in (None, "tin-codex-api-v1"):
        raise ValueError("Unknown Codex API protocol")
    # Prefix root keys; appending them after an MCP table would change their meaning.
    content = (
        'model_provider = "tin_api"\n'
        + context
        + existing
        + "\n"
        + "\n".join(
            [
                "[model_providers.tin_api]",
                'name = "Tin run-bound OpenAI API"',
                f"base_url = {json.dumps(url)}",
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "supports_websockets = false",
                "request_max_retries = 0",
                "stream_max_retries = 0",
                "[model_providers.tin_api.env_http_headers]",
                '"X-Tin-Codex-Grant" = "TIN_CODEX_API_GRANT"',
                "",
            ]
        )
    )
    content += worker_shell_policy(env)
    tomllib.loads(content)
    path.write_text(content)
    path.chmod(0o600)


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        print("TIN_CODEX_API_READY_V1")
    elif sys.argv[1:] == ["--check-v2"]:
        print("TIN_CODEX_API_READY_V2")
    elif sys.argv[1:] == ["--check-v3"]:
        print("TIN_CODEX_API_READY_V3")
    elif sys.argv[1:] == ["--check-v4"]:
        print("TIN_CODEX_API_READY_V4")
    elif sys.argv[1:] == ["--check-v5"]:
        print("TIN_CODEX_API_READY_V5")
    else:
        configure(Path("/home/user/.codex/config.toml"), os.environ)
        write_worker_proxy(WORKER_PROXY, os.environ)
