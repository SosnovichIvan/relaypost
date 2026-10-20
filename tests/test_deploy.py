import os
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from relaypost.domain import Rejected
from scripts.build_deploy_env import write_env
from scripts.check_release import check_release


def test_private_env_generation(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100:synthetic_token")
    monkeypatch.setenv("RELAYPOST_ENCRYPTION_KEY", key)
    monkeypatch.setenv("RELAYPOST_ALLOWED_USERS", "1, 2")
    target = tmp_path / "bot.env"
    write_env(target)
    assert target.stat().st_mode & 0o777 == 0o600
    assert "RELAYPOST_ALLOWED_USERS=1, 2\n" in target.read_text()
    assert key in target.read_text()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bad\nINJECT=1")
    with pytest.raises(ValueError):
        write_env(target)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100:synthetic_token")
    monkeypatch.setenv("RELAYPOST_ALLOWED_USERS", "1\nINJECT=1")
    with pytest.raises(ValueError):
        write_env(target)


@pytest.mark.parametrize(
    "kind,tag",
    [
        ("branch", "1.0.0"),
        ("tag", "v1.0.0"),
        ("tag", "1.0"),
        ("tag", "01.0.0"),
        ("tag", "1.0.0;echo bad"),
    ],
)
def test_release_rejects_non_semver_or_branch(kind, tag):
    with pytest.raises(ValueError):
        check_release(kind, tag)


def test_release_requires_remote_main_ancestry():
    with patch(
        "scripts.check_release.subprocess.run", return_value=subprocess.CompletedProcess([], 1)
    ):
        with pytest.raises(ValueError, match="main"):
            check_release("tag", "1.0.0")
    with patch(
        "scripts.check_release.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
    ) as run:
        check_release("tag", "1.0.0")
        assert run.call_args.args[0][-1] == "refs/remotes/origin/main"


def test_deploy_shell_syntax():
    result = subprocess.run(  # noqa: S603 - fixed bash executable and repository file
        ["/bin/bash", "-n", "deploy/deploy.sh"], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "scenario", ["success", "readiness-failure", "build-failure", "key-change", "preflight-failure"]
)
def test_isolated_deploy_and_rollback(tmp_path, scenario):
    import hashlib
    import sqlite3
    import tarfile

    root = tmp_path / "vps"
    for name in ["incoming", "releases/1.0.0/deploy", "shared", "data", "backups"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "shared/proxy_target").write_text("root@proxy.example")
    (root / "shared/proxy_ed25519").write_text("synthetic-key")
    (root / "shared/proxy_known_hosts").write_text("synthetic-known-host")
    (root / "current").symlink_to(root / "releases/1.0.0")
    (root / "releases/1.0.0/deploy/compose.yml").write_text("services: {}")
    previous = "TELEGRAM_BOT_TOKEN=100:old\nRELAYPOST_ENCRYPTION_KEY=same-key\n"
    (root / "shared/bot.env").write_text(previous)
    with sqlite3.connect(root / "data/relaypost.db") as db:
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('keep')")
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "Dockerfile").write_text("FROM scratch")
    (source / "deploy/compose.yml").write_text("services: {}")
    archive = root / "incoming/1.0.1.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "Dockerfile", arcname="Dockerfile")
        tar.add(source / "deploy", arcname="deploy")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    candidate = "TELEGRAM_BOT_TOKEN=100:new\nRELAYPOST_ENCRYPTION_KEY=same-key\n"
    if scenario == "key-change":
        candidate = candidate.replace("same-key", "different-key")
    (root / "incoming/1.0.1.env").write_text(candidate)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # macOS lacks Linux util-linux/coreutils. Keep the real operations in CI;
    # local adapters still perform the actual fcntl lock and SHA-256 computation.
    helpers = {
        "flock": "import fcntl,sys; fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)",
        "sha256sum": (
            "import hashlib,sys; "
            "print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest()+'  '+sys.argv[1])"
        ),
    }
    for name, code in helpers.items():
        if shutil.which(name) is None:
            helper = fake_bin / name
            helper.write_text("#!" + sys.executable + "\n" + code + "\n")
            helper.chmod(0o700)
    docker = fake_bin / "docker"
    docker.write_text("""#!/bin/sh
printf '%s %s\\n' "$RELAYPOST_IMAGE" "$*" >> "$DEPLOY_TEST_LOG"
if [ "$1" = build ] && [ "$DEPLOY_TEST_SCENARIO" = build-failure ]; then exit 2; fi
if [ "$1" = run ] && [ "$DEPLOY_TEST_SCENARIO" = preflight-failure ]; then exit 3; fi
if [ "$1" = inspect ]; then
  if [ "$DEPLOY_TEST_SCENARIO" = readiness-failure ]; then echo unhealthy; else echo healthy; fi
fi
case "$*" in *"ps --quiet bot"*) echo candidate;; esac
""")
    docker.chmod(0o700)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o700)
    log = tmp_path / "commands.log"
    result = subprocess.run(  # noqa: S603 - repository deploy script against isolated fake Docker
        ["/bin/bash", "deploy/deploy.sh", "1.0.1", checksum],
        env={
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "RELAYPOST_ROOT": str(root),
            "DEPLOY_TEST_LOG": str(log),
            "DEPLOY_TEST_SCENARIO": scenario,
        },
        capture_output=True,
        check=False,
    )
    if scenario == "success":
        assert result.returncode == 0, result.stderr.decode()
        assert (root / "current").resolve().name == "1.0.1"
        assert (root / "shared/bot.env").read_text() == candidate
        assert (root / "shared/bot.env").stat().st_mode & 0o777 == 0o600
        assert not (root / "incoming/1.0.1.env").exists()
        backup = next((root / "backups").glob("*/relaypost.db"))
        with sqlite3.connect(backup) as db:
            assert db.execute("SELECT value FROM sample").fetchone()[0] == "keep"
    else:
        assert result.returncode != 0
        assert (root / "current").resolve().name == "1.0.0"
        assert (root / "shared/bot.env").read_text() == previous
        if scenario == "readiness-failure":
            assert "relaypost:1.0.0 compose" in log.read_text()
        elif log.exists():
            assert "stop bot" not in log.read_text()
    assert b"same-key" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "proxy,valid",
    [
        ("http://user:p%24ss@proxy.example:3128", False),
        ("http://proxy.example:3128", True),
        ("socks5://proxy.example:1080", False),
        ("http://proxy.example:invalid", False),
        ("http://proxy.example:80\nINJECT=1", False),
        ("http://proxy.example:80#fragment", False),
    ],
)
def test_proxy_environment(tmp_path, monkeypatch, proxy, valid):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "100:synthetic_token")
    monkeypatch.setenv("RELAYPOST_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RELAYPOST_ALLOWED_USERS", "")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", proxy)
    target = tmp_path / "bot.env"
    if valid:
        write_env(target)
        assert "TELEGRAM_PROXY_URL=" + proxy + "\n" in target.read_text()
    else:
        with pytest.raises(Rejected, match="proxy"):
            write_env(target)
        assert not target.exists()
