#!/usr/bin/env python3
"""Root-owned boundary for the opt-in isolated Codex procedure image.

Codex and its native OAuth cache remain the controller. Its built-in tools execute
through exec-server as tin-work. No worker-controlled command runs as the controller,
including verification. This helper never implements model authentication.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

WORKER = "tin-work"
CONTROLLER = "user"
ROOTS = (Path("/home/user/project"), Path("/home/user/state"))
EXEC_URL = "ws://127.0.0.1:8788"
# Per workspace root, counting directories and the .git objects of the pinned commit:
# a repository snapshot of 100,000 files (repository_limits) needs about 2.2x that.
MAX_ENTRIES = 400_000
STUDIO_CONFIG = Path("/home/user/.tin-lite/studio-worker.json")
PROXY_CONFIG = Path("/home/user/.tin-lite/proxy-worker.json")
PROXY_KEYS = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"}
)
# Another agent harness (Tin's agent_harness): Google's Antigravity CLI, pinned in the image.
AGENT_CONFIG = Path("/home/user/.tin-lite/agent-worker.json")
AGENT_BINARY = Path("/usr/local/bin/agy")
AGENT_VERSION = "1.5.16"
AGENT_HOME = Path("/home/tin-work")
AGENT_SKILLS = Path("/home/user/.agents/skills")
AGENT_REQUIRED = frozenset(
    {"GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "TIN_AGENT_MODEL", "TIN_AGENT_SCHEMA"}
)
AGENT_OPTIONAL = frozenset(
    {"TIN_RUN_TOOLS_URL", "TIN_RUN_TOOLS_GRANT", "TIN_RUN_ID", "TIN_AGENT_STATE_DIR"}
)
# The harness's own telemetry and self-update hosts, and Gemini itself, go direct instead of
# through the run's proxy, so the egress fence refuses them: the harness reaches Gemini only
# through Tin's relay, reports nothing to Google and never replaces its pinned binary.
AGENT_FENCED_HOSTS = (
    "play.googleapis.com",
    "generativelanguage.googleapis.com",
    "antigravity-cli-auto-updater-974169037036.us-central1.run.app",
)


def trusted_config(path: Path, label: str) -> dict | None:
    """A small JSON file only the controller can have written, or None when absent."""
    if not path.exists():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != pwd.getpwnam(CONTROLLER).pw_uid
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 8192
    ):
        raise RuntimeError(f"invalid trusted {label} configuration")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or any(
        not isinstance(v, str) or not v or "\x00" in v or "\n" in v for v in value.values()
    ):
        raise RuntimeError(f"invalid trusted {label} configuration")
    return value


def studio_environment() -> list[str]:
    """Delegate only this run's bounded voice capability, never model/storage auth."""
    value = trusted_config(STUDIO_CONFIG, "Studio")
    if value is None:
        return []
    if set(value) != {"TIN_RUN_TOOLS_URL", "TIN_RUN_TOOLS_GRANT"}:
        raise RuntimeError("invalid Studio voice capability")
    if not value["TIN_RUN_TOOLS_URL"].startswith("https://"):
        raise RuntimeError("Studio voice requires HTTPS")
    return [f"{key}={value[key]}" for key in sorted(value)]


def proxy_environment() -> list[str]:
    """This run's forward-proxy grant, so commands can reach public pages through it."""
    value = trusted_config(PROXY_CONFIG, "proxy")
    if value is None:
        return []
    if set(value) != PROXY_KEYS or not value["HTTPS_PROXY"].startswith("https://"):
        raise RuntimeError("invalid run proxy")
    return [f"{key}={value[key]}" for key in sorted(value)]


def protect_runtime() -> None:
    # E2B's finalization recursively makes /usr/local world-writable *after* image
    # build steps. Harden at runtime, before starting any author process. Never follow
    # symlinks out of this exact installation tree.
    for parent, directories, files in os.walk("/usr/local", followlinks=False):
        for path in (Path(parent), *(Path(parent) / name for name in directories + files)):
            info = path.lstat()
            if info.st_mode & 0o022 and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o022)


def agent_values() -> dict[str, str]:
    """This run's harness settings, written only by the controller. The model grant in it is
    the run's: it expires with the run, buys only its pinned model and stops at its ceiling."""
    value = trusted_config(AGENT_CONFIG, "agent")
    if value is None or not AGENT_REQUIRED <= set(value) <= AGENT_REQUIRED | AGENT_OPTIONAL:
        raise RuntimeError("invalid agent configuration")
    url = value["GOOGLE_GEMINI_BASE_URL"]
    if (
        not url.startswith("https://")
        or "/internal/agent-api/" not in url
        or not url.endswith("/gemini")
    ):
        raise RuntimeError("invalid agent model route")
    model = value["TIN_AGENT_MODEL"]
    if not model.startswith("gemini-") or not all(c.isalnum() or c in ".-" for c in model):
        raise RuntimeError("invalid agent model")
    schema = Path(value["TIN_AGENT_SCHEMA"])
    if schema.parent != Path("/home/user/.agents") or not schema.is_file():
        raise RuntimeError("invalid agent result schema")
    if ("TIN_RUN_TOOLS_URL" in value) != ("TIN_RUN_TOOLS_GRANT" in value) or not value.get(
        "TIN_RUN_TOOLS_URL", "https://"
    ).startswith("https://"):
        raise RuntimeError("invalid agent run tools")
    if value.get("TIN_AGENT_STATE_DIR", "/home/user/project") not in {
        "/home/user/project",
        "/home/user/state",
    }:
        raise RuntimeError("invalid agent state directory")
    return value


def prepare_agent(value: dict[str, str]) -> None:
    """The worker's harness home: settings, Tin's skills (read-only) and the run's tools."""
    worker = pwd.getpwnam(WORKER)
    config = AGENT_HOME / ".gemini" / "config"
    cli = AGENT_HOME / ".gemini" / "antigravity-cli"
    for path in (AGENT_HOME / ".gemini", config, cli, AGENT_HOME / "scratch"):
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, worker.pw_uid, worker.pw_gid)
        os.chmod(path, 0o755)  # noqa: S103 — the worker's own harness home
    skills = config / "skills"
    if skills.exists() or skills.is_symlink():
        raise RuntimeError("agent skills already exist")
    # A copy the worker can read and not change: instructions are Tin's, not the agent's.
    shutil.copytree(AGENT_SKILLS, skills, symlinks=False)
    for parent, _directories, files in os.walk(skills):
        os.chown(parent, 0, 0)
        os.chmod(parent, 0o755)  # noqa: S103 — readable, never writable, instructions
        for name in files:
            os.chown(Path(parent) / name, 0, 0)
            os.chmod(Path(parent) / name, 0o644)
    settings = {
        "modelProvider": "gemini",
        "toolPermission": "always-proceed",
        "allowNonWorkspaceAccess": True,
        "enableTerminalSandbox": False,
    }
    servers = {}
    if "TIN_RUN_TOOLS_URL" in value:
        servers["tin-run"] = {
            "serverUrl": value["TIN_RUN_TOOLS_URL"],
            "headers": {"Authorization": f"Bearer {value['TIN_RUN_TOOLS_GRANT']}"},
            "timeoutSeconds": 300,
        }
    for path, content in (
        (cli / "settings.json", settings),
        (config / "mcp_config.json", {"mcpServers": servers}),
    ):
        path.write_text(json.dumps(content))
        os.chown(path, worker.pw_uid, worker.pw_gid)
        os.chmod(path, 0o600)


def agent_command(value: dict[str, str]) -> list[str]:
    state = value.get("TIN_AGENT_STATE_DIR", "/home/user/project")
    command = [
        str(AGENT_BINARY),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        value["TIN_AGENT_MODEL"],
        "--dangerously-skip-permissions",
        "--json-schema",
        value["TIN_AGENT_SCHEMA"],
    ]
    for directory in (state, "/home/user/repos", str(AGENT_HOME / "scratch")):
        if directory != "/home/user/project" and Path(directory).is_dir():
            command += ["--add-dir", directory]
    environment = [
        f"GEMINI_API_KEY={value['GEMINI_API_KEY']}",
        f"GOOGLE_GEMINI_BASE_URL={value['GOOGLE_GEMINI_BASE_URL']}",
        "AGY_CLI_DISABLE_AUTO_UPDATE=true",
        # Google refuses some requests outright (429/503) and bills nothing for them; the
        # relay admits a bounded number of identical retries.
        "AGY_CLI_MODEL_API_MAX_RETRIES=4",
    ]
    if run_id := value.get("TIN_RUN_ID"):
        environment.append(f"TIN_RUN_ID={run_id}")
    return worker_command(*environment, *command, fenced_hosts=AGENT_FENCED_HOSTS)


def worker_command(*command: str, fenced_hosts: tuple[str, ...] = ()) -> list[str]:
    # An allowlist, not an expanding credential denylist. No broker, storage, provider or
    # integration credential reaches commands or repository build hooks. The only grants
    # are this run's forward-proxy grant and, in Studio, its voice capability.
    return [
        "/usr/bin/setpriv",
        f"--reuid={WORKER}",
        f"--regid={WORKER}",
        "--init-groups",
        "--no-new-privs",
        "--bounding-set=-all",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        "HOME=/home/tin-work",
        "GIT_CONFIG_COUNT=2",
        "GIT_CONFIG_KEY_0=safe.directory",
        "GIT_CONFIG_VALUE_0=/home/user/project",
        "GIT_CONFIG_KEY_1=safe.directory",
        "GIT_CONFIG_VALUE_1=/home/user/state",
        *(
            f"{entry},{','.join(fenced_hosts)}"
            if fenced_hosts and entry.split("=", 1)[0] in {"NO_PROXY", "no_proxy"}
            else entry
            for entry in proxy_environment()
        ),
        *studio_environment(),
        *command,
    ]


def entries(root: Path):
    """No links/devices/FIFOs: trusted final readers must never follow an author link.

    Called before author execution and after every author process has been killed.
    This is deliberately not a racy attempt to validate an actively changing tree.
    """
    count = 0
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        count += 1
        if count > MAX_ENTRIES:
            raise RuntimeError("isolated workspace has too many entries")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise RuntimeError("isolated workspace contains a non-regular path")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise RuntimeError("isolated workspace contains a hard-linked file")
        yield path, info
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())


def prepare() -> None:
    protect_runtime()
    author = pwd.getpwnam(WORKER)
    controller = pwd.getpwnam(CONTROLLER)
    if author.pw_uid == 0 or author.pw_uid == controller.pw_uid:
        raise RuntimeError("isolated workspace needs a distinct unprivileged user")
    os.chmod("/home/user", 0o711)  # noqa: S103 — traverse to explicit shared workspaces only
    for value in (".codex", ".tin-lite"):
        path = Path("/home/user") / value
        path.mkdir(exist_ok=True)
        os.chown(path, controller.pw_uid, controller.pw_gid)
        os.chmod(path, 0o700)
    control_dir = Path("/home/user/.tin-lite/controller")
    control_dir.mkdir(exist_ok=True)
    os.chown(control_dir, controller.pw_uid, controller.pw_gid)
    os.chmod(control_dir, 0o700)
    git_config = Path("/home/user/.gitconfig")
    if git_config.exists():
        os.chmod(git_config, 0o600)
    for root in ROOTS:
        if not root.exists():
            continue
        for path, info in entries(root):
            relative = path.relative_to(root)
            trusted = not relative.parts or relative.parts[0] == ".git"
            owner = controller if trusted else author
            os.chown(path, owner.pw_uid, owner.pw_gid)
            if path == root:
                # The worker can create files, but cannot replace the controller's .git.
                os.chmod(path, 0o1777)  # noqa: S103 — sticky bit protects controller-owned .git
            elif trusted:
                os.chmod(path, 0o755 if stat.S_ISDIR(info.st_mode) else 0o644)
            else:
                os.chmod(
                    path, 0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o644
                )


def worker_pids() -> list[int]:
    uid = pwd.getpwnam(WORKER).pw_uid
    result = []
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                if path.stat().st_uid == uid:
                    # Zombies cannot change the filesystem and need their parent to reap.
                    if path.joinpath("stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
                        result.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
    return result


def freeze() -> None:
    # Stop first so descendants cannot keep forking while we remove the author UID.
    # Include detached processes, not merely the exec-server process group.
    for _ in range(10):
        for pid in worker_pids():
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
    for _ in range(10):
        pids = worker_pids()
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.01)
    if worker_pids():
        raise RuntimeError("isolated workspace still has author processes")
    # Validate all workspaces before changing any ownership. The author may have
    # created private (0700/0600) directories/files under the runner's umask. Once
    # every author process is gone, hand the validated tree back to the controller
    # for result reads and Git checkpointing; do not relax the live worker boundary.
    frozen = [entry for root in ROOTS if root.exists() for entry in entries(root)]
    controller = pwd.getpwnam(CONTROLLER)
    for path, info in frozen:
        os.chown(path, controller.pw_uid, controller.pw_gid)
        mode = (stat.S_IMODE(info.st_mode) & 0o777 & ~0o022) | 0o600
        if stat.S_ISDIR(info.st_mode):
            mode |= 0o100
        os.chmod(path, mode)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("isolated helper requires the trusted launcher")
    action, *args = sys.argv[1:]
    if action == "check" and not args:
        protect_runtime()
        if pwd.getpwnam(WORKER).pw_uid in {0, pwd.getpwnam(CONTROLLER).pw_uid}:
            raise RuntimeError("isolated execution user is invalid")
        for value in ("isolated-procedure", "codex_usage.py"):
            info = Path("/opt/tin-lite", value).lstat()
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise RuntimeError("isolated runtime resources are not protected")
        version = subprocess.check_output(["/usr/local/bin/codex", "--version"], text=True)
        if version.strip() != "codex-cli 0.159.1":
            raise RuntimeError("isolated runtime requires the pinned Codex CLI")
        print("TIN_ISOLATION_READY_V1")
    elif action == "check-agent" and not args:
        protect_runtime()
        info = AGENT_BINARY.lstat()
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise RuntimeError("agent harness is not protected")
        version = subprocess.check_output(  # noqa: S603 — the fixed pinned binary
            [str(AGENT_BINARY), "--version"],
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/root", "AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
            timeout=30,
        )
        if version.strip() != AGENT_VERSION:
            raise RuntimeError("isolated runtime requires the pinned agent harness")
        print(f"TIN_AGENT_READY_V1 antigravity {AGENT_VERSION}")
    elif action == "agent" and not args:
        # The whole harness runs as the worker; its stdin/stdout are the controller's pipes.
        value = agent_values()
        prepare_agent(value)
        os.chdir("/home/user/project")
        os.execv("/usr/bin/setpriv", agent_command(value))  # noqa: S606 — drops identity first
    elif action == "prepare" and not args:
        prepare()
    elif action == "serve" and not args:
        os.execv(  # noqa: S606 — fixed setpriv binary drops identity before author execution
            "/usr/bin/setpriv",
            worker_command("/usr/local/bin/codex", "exec-server", "--listen", EXEC_URL),
        )
    elif action == "freeze" and not args:
        freeze()
    elif action == "diagram-check" and len(args) == 2:
        candidate, output = map(Path, args)
        if (
            not any(candidate.is_relative_to(root) for root in ROOTS)
            or ".." in candidate.parts
            or output.parent.parent != Path("/tmp")  # noqa: S108 — constrained ephemeral previews
            or not output.parent.name.startswith("tin-diagram-review-")
            or not output.name.isdecimal()
        ):
            raise RuntimeError("invalid diagram preview paths")
        # Read the author's files as the author, never as root/controller. Returned
        # diagnostics are advisory; publication uses a separate clean sandbox.
        result = subprocess.run(  # noqa: S603 — fixed checker, unprivileged execution
            worker_command(
                "/usr/bin/env",
                "node",
                "/opt/tin-lite/diagram/scripts/check_diagram.mjs",
                "check",
                str(candidate),
                "--out",
                str(output),
            ),
            cwd=ROOTS[0],
            timeout=90,
            check=False,
        )
        raise SystemExit(result.returncode)
    elif action == "verify" and len(args) == 1:
        # Verification may run repository code. Its output is not a control channel.
        result = subprocess.run(  # noqa: S603 — intentional untrusted execution, after setpriv
            worker_command("/bin/bash", "--noprofile", "--norc", "-c", args[0]),
            cwd=ROOTS[0],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("isolated procedure verification failed")
    else:
        raise RuntimeError("unsupported isolated helper action")


if __name__ == "__main__":
    main()
