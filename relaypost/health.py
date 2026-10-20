"""Local readiness: a successful Telegram polling response within 90 seconds."""

import os
import time
from pathlib import Path


def heartbeat_path() -> Path:
    default = Path(os.environ.get("RELAYPOST_DATABASE", "relaypost.db")).with_suffix(".heartbeat")
    return Path(os.environ.get("RELAYPOST_HEARTBEAT", str(default)))


def clear_readiness() -> None:
    heartbeat_path().unlink(missing_ok=True)


def mark_ready() -> None:
    heartbeat_path().touch(mode=0o600)


def healthy() -> bool:
    try:
        age = time.time() - heartbeat_path().stat().st_mtime
        return 0 <= age <= 90
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
