"""Exercise only the installer's runtime-env block, without root or service changes."""

import subprocess
from pathlib import Path

import pytest


def test_deploy_requires_explicit_cloud_project_before_any_side_effect(tmp_path):
    script = Path("infra/deploy.sh").resolve()
    result = subprocess.run(  # noqa: S603 — only the fail-closed preflight may run
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Set TIN_LITE_GCP_PROJECT to your deployment project" in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("existing", [False, True])
def test_install_preserves_operator_sandbox_selection(tmp_path, existing):
    runtime = tmp_path / "runtime.env"
    if existing:
        runtime.write_text(
            "TIN_LITE_PUBLIC_URL=https://old.example\n"
            "TIN_LITE_E2B_TEMPLATE=custom-default\n"
            "TIN_LITE_E2B_ISOLATED_TEMPLATE=custom-isolated\n"
            "TIN_LITE_E2B_BROWSER_TEMPLATE=custom-browser\n"
            "TIN_LITE_TEST_SETTING=keep-me\n"
            "TIN_LITE_MCP_OAUTH_CLIENT_IDS=registered_tin_client,https://agent.example/client.json\n"
        )
    installer = Path("infra/install_switchboard.sh").read_text()
    block = installer.split("runtime_env=/etc/tin-lite/runtime.env\n", 1)[1].split(
        "chmod 640 /etc/tin-lite/runtime.env", 1
    )[0]
    block = "runtime_env=/etc/tin-lite/runtime.env\n" + block
    block = block.replace("/etc/tin-lite/", f"{tmp_path}/")
    subprocess.run(  # noqa: S603 — trusted installer block in a test-owned directory
        ["/bin/bash", "-euc", block],
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "product_host": "new.example",
            "technical_host": "technical.example",
            "TIN_LITE_STATIC_IP": "192.0.2.1",
        },
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in runtime.read_text().splitlines())
    assert values["TIN_LITE_PUBLIC_URL"] == "https://new.example"
    assert values["TIN_LITE_PROXY_URL"] == "https://tin-lite-proxy.192-0-2-1.sslip.io:8888"
    assert values["TIN_LITE_PROXY_GRANT_DIR"] == "/var/lib/tin-lite-proxy-grants"
    assert runtime.read_text().count("\nTIN_LITE_E2B_TEMPLATE=") == 1
    if existing:
        assert values["TIN_LITE_E2B_TEMPLATE"] == "custom-default"
        assert values["TIN_LITE_E2B_ISOLATED_TEMPLATE"] == "custom-isolated"
        assert values["TIN_LITE_E2B_BROWSER_TEMPLATE"] == "custom-browser"
        assert values["TIN_LITE_TEST_SETTING"] == "keep-me"
        assert values["TIN_LITE_MCP_OAUTH_CLIENT_IDS"] == (
            "registered_tin_client,https://agent.example/client.json"
        )
        assert len(list(tmp_path.glob("runtime.env.before-*"))) == 1
    else:
        assert values["TIN_LITE_E2B_TEMPLATE"] == "tin-lite-codex"


@pytest.mark.parametrize("full_switch", [False, True])
def test_repeat_install_preserves_separate_app_origin_and_other_authorized_parties(
    tmp_path, full_switch
):
    runtime = tmp_path / "runtime.env"
    runtime.write_text(
        "TIN_LITE_PUBLIC_URL=https://lite.example\n"
        "TIN_LITE_APP_URL=https://app.example\n"
        "CLERK_AUTHORIZED_PARTIES=https://lite.example,https://parent.example\n"
        "TIN_LITE_BILLING_ENABLED=true\n"
        + ("TIN_LITE_LEGACY_PUBLIC_URL=https://lite.example\n" if full_switch else "")
    )
    installer = Path("infra/install_switchboard.sh").read_text()
    domains = installer.split('technical_host="', 1)[1].split("cat > /etc/caddy/Caddyfile", 1)[0]
    domains = 'technical_host="' + domains
    block = installer.split("runtime_env=/etc/tin-lite/runtime.env\n", 1)[1].split(
        "chmod 640 /etc/tin-lite/runtime.env", 1
    )[0]
    block = domains + "runtime_env=/etc/tin-lite/runtime.env\n" + block
    block = block.replace("/etc/tin-lite/", f"{tmp_path}/")
    result = subprocess.run(  # noqa: S603 — test-owned configuration paths
        ["/bin/bash", "-euc", block + '\nprintf "%s" "$public_hosts"'],
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TIN_LITE_PRODUCT_HOST": "app.example" if full_switch else "lite.example",
            "TIN_LITE_STATIC_IP": "192.0.2.1",
        },
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in runtime.read_text().splitlines())
    assert result.stdout == (
        "app.example, lite.example" if full_switch else "lite.example, app.example"
    )
    assert values["TIN_LITE_PUBLIC_URL"] == (
        "https://app.example" if full_switch else "https://lite.example"
    )
    assert values["TIN_LITE_APP_URL"] == "https://app.example"
    assert values["TIN_LITE_BILLING_ENABLED"] == "true"
    parties = values["CLERK_AUTHORIZED_PARTIES"].split(",")
    assert len(parties) == len(set(parties))
    assert {"https://lite.example", "https://app.example", "https://parent.example"} <= set(parties)
    if full_switch:
        assert values["TIN_LITE_LEGACY_PUBLIC_URL"] == "https://lite.example"
        assert {"app.example", "lite.example"} <= set(
            values["TIN_LITE_EGRESS_ALLOW_HOSTS"].split(",")
        )


def _run_gcloud_connect(tmp_path, exit_codes):
    """Run deploy.sh's retry helper against a fake gcloud that exits with each code in turn."""
    script = Path("infra/deploy.sh").read_text()
    helper = "gcloud_connect() {" + script.split("gcloud_connect() {", 1)[1].split("\n}\n", 1)[0]
    codes = tmp_path / "codes"
    codes.write_text("".join(f"{code}\n" for code in exit_codes))
    calls = tmp_path / "calls"
    fake = tmp_path / "gcloud"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {calls}\n'
        f"code=$(head -n 1 {codes}); sed -i 1d {codes}\n"
        'exit "${code}"\n'
    )
    fake.chmod(0o755)
    result = subprocess.run(  # noqa: S603 — the helper runs only the fake gcloud above
        [
            "/bin/bash",
            "-euo",
            "pipefail",
            "-c",
            f"sleep() {{ :; }}\ninstance=test-host\n{helper}\n}}\n"
            "gcloud_connect compute scp a b:/c",
        ],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_deploy_retries_a_refused_connection(tmp_path):
    result, calls = _run_gcloud_connect(tmp_path, [255, 0])
    assert result.returncode == 0
    assert calls == ["compute scp a b:/c", "compute scp a b:/c"]
    assert "could not connect to test-host (attempt 1 of 3)" in result.stderr


def test_deploy_stops_after_three_refused_connections(tmp_path):
    result, calls = _run_gcloud_connect(tmp_path, [255, 255, 255, 0])
    assert result.returncode == 255
    assert len(calls) == 3


def test_deploy_does_not_retry_other_failures(tmp_path):
    result, calls = _run_gcloud_connect(tmp_path, [1, 0])
    assert result.returncode == 1
    assert len(calls) == 1


def test_deploy_never_retries_activation():
    script = Path("infra/deploy.sh").read_text()
    activation = script.split("activate_release.sh takes the host lock", 1)[1]
    assert "\ngcloud compute ssh" in activation
    assert "gcloud_connect" not in activation
