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


def studio_environment() -> list[str]:
    """Delegate only this run's bounded voice capability, never model/storage auth."""
    if not STUDIO_CONFIG.exists():
        return []
    info = STUDIO_CONFIG.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != pwd.getpwnam(CONTROLLER).pw_uid
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 8192
    ):
        raise RuntimeError("invalid trusted Studio configuration")
    value = json.loads(STUDIO_CONFIG.read_text())
    if set(value) != {"TIN_RUN_TOOLS_URL", "TIN_RUN_TOOLS_GRANT"} or any(
        not isinstance(v, str) or not v or "\x00" in v or "\n" in v for v in value.values()
    ):
        raise RuntimeError("invalid Studio voice capability")
    if not value["TIN_RUN_TOOLS_URL"].startswith("https://"):
        raise RuntimeError("Studio voice requires HTTPS")
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


def worker_command(*command: str) -> list[str]:
    # An allowlist, not an expanding credential denylist. No broker, proxy, storage,
    # provider or integration credential reaches commands or repository build hooks.
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
        if version.strip() != "codex-cli 0.156.1":
            raise RuntimeError("isolated runtime requires the pinned Codex CLI")
        print("TIN_ISOLATION_READY_V1")
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
