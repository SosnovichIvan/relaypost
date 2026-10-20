# Telegram через vpn_proxy_server

Используется существующий закрытый Squid `10.66.66.1:3128`, как в planerBot.
На proxy VPS добавлен отдельный публичный ключ RelayPost; имеющиеся ключи,
конфигурации Squid/WireGuard/firewall и клиенты не изменяются.

## Путь соединения

`RelayPost → telegram-proxy:19840 → SSH → Squid 10.66.66.1:3128 → api.telegram.org:443`.
HTTPS CONNECT сохраняет проверку TLS-сертификата Telegram. Прокси не видит
расшифрованные запросы Bot API. Ключ разрешает только port forwarding к указанному
Squid (`restrict,port-forwarding,permitopen=...,command=/bin/false`), без shell/PTY.

`relaypost.network.create_client` — общий transport для polling, API-публикаций,
бизнес-историй и скачивания файлов Telegram. HTTPX mount выбран только для
`https://api.telegram.org`; API ВК, MAX и их upload hosts используют прямой transport.
Глобальные HTTP_PROXY/HTTPS_PROXY не наследуются. При отказе прокси нет прямого
fallback для Telegram. Локально пустой TELEGRAM_PROXY_URL разрешает direct.

## Хранение доступа

На VPS бота, root-only `/opt/relaypost/shared`:

- `proxy_ed25519` (0600) — приватный ключ, созданный непосредственно на VPS;
- `proxy_ed25519.pub` — его публичная часть для authorized_keys;
- `proxy_known_hosts` — проверенный host key из существующего SSH-доступа проекта;
- `proxy_target` — SSH user@host закрытого proxy VPS.

Ключ не копируется в Git, Docker image, CI artifacts или GitHub logs.
Контейнер получает ключ/known_hosts как read-only Compose secrets.
Порт 19840 слушает только в Docker-сети: `ports` отсутствует.
SSH включает BatchMode, IdentitiesOnly, StrictHostKeyChecking, ExitOnForwardFailure
и keepalive; restart policy восстанавливает оборванный tunnel.
VPS бота уже подготовлен; deployment pipeline повторно использует эти host files.
На новом VPS перед первым deploy нужен отдельный bootstrap этих файлов и ключа.

## Готовность и выпуск

`deploy.sh` собирает два target Dockerfile: bot и tunnel. После запуска
telegram-proxy и TCP healthcheck выполняется отдельный контейнерный
`python -m relaypost.network` в той же Docker-сети. Он получает только внутренний
TELEGRAM_PROXY_URL, без bot token, ключа шифрования или data volume.
Preflight выполняет HEAD к официальному API с TLS, принимает только 2xx/3xx;
ошибка завершает deploy до остановки рабочего poller/SQLite backup.
Затем обычный bot healthcheck требует успешного getUpdates за последние 90s.
Открытый TCP listener сам по себе не считается доказательством доступа к Telegram.

При обновлении tunnel контейнера возможен краткий разрыв соединения. In-flight
публикация может остаться unknown; автоматического повторения внешних эффектов нет.
После смены SSH-доступа проверяйте preflight до выпуска.

## Проверки

Unit/contract tests проверяют URL validation без раскрытия конфигурации,
Telegram-only routing, media download routing, отсутствие direct fallback,
нормализацию ProxyError, закрытие clients и token-free preflight.
Изолированный deployment harness проверяет, что провал preflight не останавливает
предыдущий poller и не меняет его env/БД. CI собирает оба image targets.

2026-10-20: с VPS RelayPost через новый ограниченный ключ закрытый Squid вернул
Telegram HTTP 302 при включённой TLS verification. Временный localhost-туннель
после проверки закрыт; постоянная конфигурация находится в Compose.

[HTTPX proxies and mounts](https://www.python-httpx.org/advanced/proxies/) —
официальный контракт транспорта; закреплённая версия проекта httpx 0.28.1.
