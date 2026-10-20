"""Provider-independent content rules."""

from typing import Any


class Rejected(Exception):
    """Safe, user-visible validation or provider rejection."""


class Uncertain(Rejected):
    """A remote write may have succeeded; never retry automatically."""


def formats(platform: str, kind: str, media: str) -> list[str]:
    if platform == "telegram-business":
        return ["story"] if kind == "business" and media in {"photo", "video"} else []
    if platform == "telegram":
        return ["post" if kind == "channel" else "message"]
    if platform == "max":
        return ["post" if kind == "channel" else "message"]
    if platform == "vk":
        return (["story"] if media in {"photo", "video"} else []) if kind == "story" else ["post"]
    return []


def validate_content(content: dict[str, Any], platform: str, mode: str) -> None:
    # Conservative common limit for captions; Telegram counts UTF-16 units.
    text = content["text"]
    limit = (
        1000 if content["media"] != "text" else {"telegram": 4096, "max": 4000}.get(platform, 16000)
    )
    if len(text.encode("utf-16-le")) // 2 > limit:
        raise Rejected(f"Слишком длинный текст: максимум {limit} символов.")
    if platform == "vk" and mode == "story" and text:
        raise Rejected("ВК API не принимает подпись истории. Разместите текст на самом фото/видео.")
    if mode == "story" and content["media"] not in {"photo", "video"}:
        raise Rejected("История требует фото или видео.")
