"""Write deploy credentials to a private file; never emit secret values."""

import os
import re
from pathlib import Path

from relaypost.network import validate_proxy
from relaypost.telegram_user import TelegramUsers
from relaypost.user_login import login_origin


def write_env(path: Path) -> None:
    names = ("TELEGRAM_BOT_TOKEN", "RELAYPOST_ENCRYPTION_KEY", "RELAYPOST_ALLOWED_USERS")
    values = {name: os.environ.get(name, "") for name in names}
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", values["TELEGRAM_BOT_TOKEN"]):
        raise ValueError("Missing or malformed Telegram bot token")
    from cryptography.fernet import Fernet

    Fernet(values["RELAYPOST_ENCRYPTION_KEY"].encode())
    if not re.fullmatch(r"(?:\d+(?:,\s*\d+)*)?", values["RELAYPOST_ALLOWED_USERS"]):
        raise ValueError("Malformed user allowlist")
    values["TELEGRAM_PROXY_URL"] = validate_proxy(os.environ.get("TELEGRAM_PROXY_URL", ""))
    origin = os.environ.get("RELAYPOST_LOGIN_ORIGIN", "")
    if origin:
        values["RELAYPOST_LOGIN_ORIGIN"] = login_origin(origin)
        values["TELEGRAM_API_ID"] = str(int(os.environ.get("TELEGRAM_API_ID", "0")))
        values["TELEGRAM_API_HASH"] = os.environ.get("TELEGRAM_API_HASH", "")
        TelegramUsers(
            int(values["TELEGRAM_API_ID"]),
            values["TELEGRAM_API_HASH"],
            values["TELEGRAM_PROXY_URL"],
        )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(f"{name}={value}" for name, value in values.items()) + "\n")
    os.chmod(path, 0o600)


if __name__ == "__main__":
    write_env(Path(os.environ["RELAYPOST_ENV_OUTPUT"]))
