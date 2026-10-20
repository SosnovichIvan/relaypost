"""Media uploads to provider-issued HTTPS endpoints with a host allowlist."""

from typing import Any
from urllib.parse import urlsplit

import httpx

from relaypost.domain import Rejected


def upload_url(url: str, platform: str) -> str:
    parsed = urlsplit(url)
    roots = {
        "vk": ("vk.com", "userapi.com", "vkuser.net", "vkuserlive.ru"),
        "max": ("oneme.ru", "okcdn.ru", "max.ru"),
    }[platform]
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
        or not any(
            parsed.hostname == root or (parsed.hostname or "").endswith("." + root)
            for root in roots
        )
    ):
        raise Rejected("Площадка вернула недоверенный адрес загрузки.")
    return url


async def upload(
    client: httpx.AsyncClient,
    url: str,
    platform: str,
    field: str,
    binary: bytes,
    photo: bool,
    *,
    token: str = "",
    json_response: bool = True,
) -> Any:
    url = upload_url(url, platform)
    headers = {"Authorization": token} if token else {}
    try:
        response = await client.post(
            url, files={field: ("media.jpg" if photo else "media.mp4", binary)}, headers=headers
        )
        if response.is_error:
            raise Rejected("Не удалось загрузить медиа на площадку.")
        return response.json() if json_response else None
    except (httpx.TransportError, ValueError) as exc:
        raise Rejected("Не удалось подтвердить загрузку медиа.") from exc
