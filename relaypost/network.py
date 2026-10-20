"""Telegram-only HTTPS CONNECT through the vpn_proxy_server SSH tunnel."""

import asyncio
import os
from urllib.parse import urlsplit

import httpx

from relaypost.domain import Rejected


def validate_proxy(value: str) -> str:
    if not value:
        return ""
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "http"
            and url.hostname
            and url.port is not None
            and 0 < url.port < 65536
            and not url.username
            and not url.password
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise Rejected("Invalid Telegram proxy configuration") from None
    return value


def create_client(proxy: str = "") -> httpx.AsyncClient:
    proxy = validate_proxy(proxy)
    mounts: dict[str, httpx.AsyncBaseTransport] = {}
    if proxy:
        mounts["https://api.telegram.org"] = httpx.AsyncHTTPTransport(
            proxy=proxy, trust_env=False, verify=True
        )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(40, connect=10),
        follow_redirects=False,
        trust_env=False,
        mounts=mounts,
    )


async def preflight() -> None:
    """Verify TLS/CONNECT without any bot token or publication side effect."""
    async with create_client(os.environ.get("TELEGRAM_PROXY_URL", "")) as client:
        try:
            response = await client.head("https://api.telegram.org/", timeout=15)
            if not 200 <= response.status_code < 400:
                raise Rejected("Telegram proxy preflight rejected")
        except httpx.TransportError:
            raise Rejected("Telegram proxy preflight unavailable") from None


def main() -> None:
    try:
        asyncio.run(preflight())
    except Rejected:
        raise SystemExit("Telegram proxy preflight failed") from None


if __name__ == "__main__":
    main()
