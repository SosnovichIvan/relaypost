"""Private login pages: no external assets, URL logging, cache or password persistence."""

import base64
import html
import io
import secrets
import sqlite3
from typing import Any

import qrcode  # type: ignore[import-untyped]
import qrcode.image.svg  # type: ignore[import-untyped]
from aiohttp import web

from relaypost.domain import Rejected
from relaypost.user_login import Login, UserLogin

HEADERS = {
    "Cache-Control": "no-store",
    # no-referrer turns native form POST Origin into null in Chromium. Only the
    # public origin may be referred; the bearer login path must never be sent.
    "Referrer-Policy": "strict-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def page(entry: Login, error: str = "") -> str:
    content = '<p role="alert">' + html.escape(error) + "</p>" if error else ""
    if entry.owner > 0:
        content += (
            f"<p>Подключение к профилю RelayPost пользователя <strong>ID {entry.owner}</strong>. "
            "Продолжайте только по ссылке из своего личного диалога с ботом. "
            "Не передавайте эту ссылку другим людям.</p>"
        )
    refresh = '<meta http-equiv="refresh" content="3">' if entry.state == "qr" else ""
    if entry.state == "new":
        content += (
            "<p>Подключение личного Telegram-аккаунта к RelayPost. Сессия даёт приложению "
            "широкий доступ к аккаунту. Мы сохраняем её зашифрованной и публикуем только "
            "после вашего подтверждения. Отозвать доступ можно в Telegram → Устройства.</p>"
            '<label for="phone">Вход без QR: номер с кодом страны</label>'
            '<input id="phone" name="phone" type="tel" autocomplete="tel" '
            'maxlength="16" placeholder="+79991234567">'
            '<button name="action" value="phone">Получить код входа</button>'
            "<p>Номер передаётся Telegram для входа. Способ доставки кода выбирает Telegram. "
            "Также можно войти через QR на другом экране:</p>"
        )
        action, label = "begin", "Показать QR-код"
    elif entry.state == "qr":
        image = qrcode.make(entry.qr, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        encoded = base64.b64encode(buffer.getvalue()).decode()
        content += (
            "<p>В Telegram откройте Настройки → Устройства → Подключить устройство "
            "и сканируйте код. Не сканируйте чужие коды. QR действует около минуты.</p>"
            '<img width="280" height="280" alt="QR-код входа в ваш Telegram" '
            f'src="data:image/svg+xml;base64,{encoded}">'
        )
        action, label = "cancel", "Отменить вход"
    elif entry.state == "code":
        content += (
            "<p>Введите код, полученный выбранным Telegram способом. "
            "Не отправляйте его в переписку с ботом. Код не сохраняется.</p>"
            '<label for="code">Код входа</label>'
            '<input id="code" name="code" type="text" inputmode="numeric" '
            'autocomplete="one-time-code" maxlength="64" required>'
        )
        action, label = "code", "Подтвердить код"
    elif entry.state == "password":
        content += (
            "<p>Telegram требует пароль двухэтапной аутентификации. Он не сохраняется.</p>"
            '<label for="password">Пароль 2FA</label>'
            '<input id="password" name="password" type="password" '
            'autocomplete="current-password" maxlength="256" required>'
        )
        action, label = "password", "Подтвердить пароль"
    elif entry.state == "confirm":
        content += (
            f"<p>Вы вошли как <strong>{html.escape(entry.profile['label'])}</strong> "
            f"(ID {entry.profile['id']}). Сохранить этот аккаунт для публикаций? "
            "Каждая отправка будет подтверждаться отдельно в боте.</p>"
        )
        action, label = "confirm", "Сохранить аккаунт"
    elif entry.state == "cancelled":
        content += "<p>Вход отменён. Закройте страницу.</p>"
        action, label = "", ""
    else:
        content += (
            "<p>Аккаунт подключён. Вернитесь в бот: /accounts, затем /discover ID.</p>"
            if entry.state == "saved"
            else "<p>Время входа истекло. Вернитесь в бот и получите новую ссылку: /login.</p>"
        )
        action, label = "", ""
    csrf = html.escape(entry.csrf, quote=True)
    if action:
        content = (
            f'<form method="post"><input type="hidden" name="csrf" value="{csrf}">'
            f'{content}<button name="action" value="{action}">{label}</button></form>'
        )
    if entry.state in {"code", "password", "confirm"}:
        content += (
            f'<form method="post"><input type="hidden" name="csrf" value="{csrf}">'
            '<button name="action" value="cancel">Отменить вход</button></form>'
        )
    return (
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + refresh
        + "<title>Подключить Telegram · RelayPost</title><style>"
        "body{font:17px/1.6 system-ui;background:#f8fafc;color:#1e293b;margin:0;padding:24px}"
        "main{max-width:520px;margin:5vh auto;background:white;padding:28px;"
        "border-radius:20px;overflow-wrap:anywhere}"
        "h1{font-size:28px}button,input{font:inherit;box-sizing:border-box;width:100%;"
        "min-height:48px;margin:10px 0;padding:12px;border-radius:10px}"
        "button{background:#2563eb;color:white;border:0;cursor:pointer}"
        "input{border:1px solid #475569}"
        ":focus-visible{outline:3px solid #ea580c;outline-offset:3px}"
        "img{max-width:100%;height:auto}label{display:block}"
        "</style><main><p>RelayPost</p><h1>Ваш Telegram-аккаунт</h1>" + content + "</main></html>"
    )


def application(login: UserLogin) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        try:
            token = request.match_info["token"]
            entry = await login.get(token)
            cookie = request.cookies.get("__Host-relaypost", "")
            if entry.browser and not secrets.compare_digest(
                cookie.encode(), entry.browser.encode()
            ):
                raise Rejected("Эта ссылка уже открыта в другом браузере. Получите новую: /login.")
            if not entry.browser:
                entry.browser = secrets.token_urlsafe(32)
            if request.method == "POST":
                form: Any = await request.post()
                if (
                    request.headers.get("Origin") != login.origin
                    or not secrets.compare_digest(
                        str(form.get("csrf", "")).encode(), entry.csrf.encode()
                    )
                    or not cookie
                ):
                    raise Rejected("Проверка страницы не пройдена. Откройте ссылку из бота заново.")
                try:
                    await login.act(
                        token,
                        str(form.get("action", "")),
                        str(form.get("password", "")),
                        phone=str(form.get("phone", "")),
                        code=str(form.get("code", "")),
                    )
                except Rejected as exc:
                    return web.Response(
                        text=page(entry, str(exc)),
                        content_type="text/html",
                        headers=HEADERS,
                        status=400,
                    )
                except sqlite3.Error:
                    return web.Response(
                        text=page(entry, "Не удалось сохранить подключение. Попробуйте позже."),
                        content_type="text/html",
                        headers=HEADERS,
                        status=503,
                    )
                if token not in login.tickets:
                    return web.Response(
                        text=page(Login(0, state="cancelled")),
                        content_type="text/html",
                        headers=HEADERS,
                    )
                raise web.HTTPSeeOther(request.path, headers=HEADERS)
            response = web.Response(text=page(entry), content_type="text/html", headers=HEADERS)
            response.set_cookie(
                "__Host-relaypost",
                entry.browser,
                secure=True,
                httponly=True,
                samesite="Strict",
                max_age=600,
                path="/",
            )
            return response
        except Rejected as exc:
            return web.Response(
                text=page(Login(0, state="expired"), str(exc)),
                content_type="text/html",
                status=403,
                headers=HEADERS,
            )

    app = web.Application(client_max_size=4096)
    app.router.add_get("/login/{token}", handle)
    app.router.add_post("/login/{token}", handle)
    return app
