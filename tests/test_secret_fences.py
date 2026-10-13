from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_no_secret_bearing_git_remote_is_written_by_sandbox_script() -> None:
    for name in ("run_task.sh", "run_procedure.sh"):
        script = (ROOT / "sandbox" / name).read_text()
        assert "https://t:" not in script
        assert "GIT_CONFIG_VALUE_0" in script
        assert 'remote add ephemeral "${TIN_EPHEMERAL_URL}"' in script
        assert "X-Tin-Auth-Version" not in script
        assert "/internal/broker/auth" not in script


def test_env_files_and_auth_material_are_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".env.local" in ignored
    assert not (ROOT / "auth.json").exists()


def test_no_openai_api_key_is_defined_in_local_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        env_text = env_path.read_text()
        assert not any(line.startswith("OPENAI_API_KEY=") for line in env_text.splitlines())


def test_luna_key_never_enters_sandbox_or_broker_paths() -> None:
    sandbox_sources = "\n".join(
        path.read_text() for path in (ROOT / "sandbox").glob("*") if path.is_file()
    )
    assert not (ROOT / "src" / "tin_lite" / "broker.py").exists()
    for forbidden in (
        "TIN_LITE_LUNA_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "TIN_LITE_INTEGRATION_CREDENTIAL_KEY",
        "TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET",
        "TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH",
        "TIN_LITE_GITHUB_WEBHOOK_SECRET",
        "FAL_KEY",
    ):
        assert forbidden in sandbox_sources


def test_github_private_key_is_installed_as_host_state_not_release_state() -> None:
    deploy = (ROOT / "infra" / "deploy.sh").read_text()
    activate = (ROOT / "infra" / "activate_release.sh").read_text()

    assert 'openssl pkey -in "${github_private_key_path}" -check -noout' in deploy
    assert '"${instance}:${upload_dir}/tin-lite-github-app.pem"' in deploy
    assert "sudo chmod 600 ${upload_dir}/tin-lite-github-app.pem" in deploy
    assert "mktemp -d /tmp/tin-deploy.XXXXXXXXXX" in deploy
    assert "sudo rm -rf ${upload_dir}" in deploy
    assert "install -m 640 -o root -g tinlite" in activate
    assert "/etc/tin-lite/github-app.pem" in activate
    assert "TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH=/etc/tin-lite/github-app.pem" in activate
    assert "trap cleanup EXIT" in activate
    assert not list(ROOT.glob("*.pem"))


def test_rollout_sessions_survive_every_sandbox_runner() -> None:
    """Codex rollouts under /home/user/.codex/sessions are captured before kill."""
    for name in ("run_task.sh", "run_procedure.sh"):
        script = (ROOT / "sandbox" / name).read_text()
        assert "/home/user/.codex" in script
        for line in script.splitlines():
            if "rm " in line:
                assert ".codex/sessions" not in line, name
                assert "rm -rf /home/user/.codex" not in line, name
    for name in ("procedure_app_server.py", "task_app_server.py"):
        bridge = (ROOT / "sandbox" / name).read_text()
        assert '"ephemeral": False' in bridge, name
        assert '"ephemeral": True' not in bridge, name
