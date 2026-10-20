# VPS deployment

Одна production-реплика RelayPost в Docker Compose. Public HTTP ports не нужны:
бот работает через Telegram long polling. Docker daemon включён в systemd;
контейнер использует restart=unless-stopped и healthcheck успешного polling.
Контейнер uid=10001, read-only root filesystem, отдельный writable data bind mount,
ограничение памяти 512 МБ, no-new-privileges, сброшенные capabilities, ротация логов.

## GitHub repository secrets

| Имя | Назначение |
| --- | --- |
| TELEGRAM_BOT_TOKEN | Управляющий Telegram-бот |
| RELAYPOST_ENCRYPTION_KEY | Постоянный Fernet key, тот же при каждом deploy |
| VPS_HOST | Адрес VPS |
| VPS_USER | SSH user |
| VPS_PASSWORD | SSH password, передаётся sshpass через окружение |
| VPS_KNOWN_HOSTS | Закреплённый SSH host key; StrictHostKeyChecking=yes |
| RELAYPOST_ALLOWED_USERS | Опциональный список user ID; пустой = публичный бот |

Значения не хранятся в Git. Runner пишет credentials в файл mode 0600,
передаёт по SSH и удаляет файл в always cleanup. Сервер хранит bot.env mode 0600
под root-only `/opt/relaypost/shared`. Root password не попадает на диск сервера
в конфигурацию приложения. GitHub шифрует Secrets; прочитать сохранённое
значение обратно через API нельзя — проверяются имена и успешная запись.

## Пайплайн

`Deploy` запускается только при push тега `MAJOR.MINOR.PATCH`.
`Verify release` сначала проверяет точный формат и ancestry коммита в свежей
удалённой main, затем запускает полный make check без production secrets.
`Deploy VPS` повторно проверяет source, создаёт git archive доверенного коммита,
формирует env и передаёт архив, SHA-256 и конфигурацию по SSH.
Теги immutable, ветки main/develop защищены PR/review/checks.
Environment production ограничен тегами. Pull requests не запускают deployment.

Первое ручное развёртывание выполняется по прямому запросу владельца из
проверенного коммита рабочей ветки; это отдельное разрешение на initial deploy,
не обход защиты GitHub и не выпуск версионного тега вне main.
Автоматические релизы начнутся после обычного пути feature → develop → main
и создания разрешённого version tag. Политику ревью не отключать ради запуска.

## Каталоги и переключение

- `/opt/relaypost/releases/VERSION` — исходники проверенной версии.
- `/opt/relaypost/current` — symlink на последнюю healthy версию.
- `/opt/relaypost/data/relaypost.db` — постоянная SQLite с owner uid=10001.
- `/opt/relaypost/shared/bot.env` — секреты; `version` — текущий deploy ID.
- `/opt/relaypost/backups/TIMESTAMP-VERSION` — SQLite backup и прежний env.
- `/opt/relaypost/incoming` — приватный staging; успешный deploy удаляет env/archive.

`deploy/deploy.sh VERSION SHA256` держит flock, проверяет checksum и неизменность
ключа шифрования, собирает новый образ до остановки старого, останавливает
старый poller, создаёт SQLite backup, переключает контейнер и ждёт health до 150s.
Health означает успешный ответ Telegram getUpdates за последние 90s;
один getMe не считается достаточной проверкой polling.
Проверка прав и доставка постов требуют отдельных живых тестовых назначений.

При ошибке кандидат останавливается, предыдущие image/env возвращаются при их
наличии. БД автоматически назад не восстанавливается: это могло бы удалить
новые данные. Миграции должны сохранять совместимость с предыдущим релизом;
для несовместимого upgrade требуется отдельный план. Удалённые публикации также
не откатываются DB-backup. Тест автоматического rollback не должен создавать
реальные публикации.

## Операции на VPS

```sh
cd /opt/relaypost/current
export RELAYPOST_IMAGE="relaypost:$(cat /opt/relaypost/shared/version)"
export RELAYPOST_TUNNEL_IMAGE="relaypost-tunnel:$(cat /opt/relaypost/shared/version)"
export RELAYPOST_PROXY_KEY_FILE=/opt/relaypost/shared/proxy_ed25519
export RELAYPOST_PROXY_KNOWN_HOSTS_FILE=/opt/relaypost/shared/proxy_known_hosts
export RELAYPOST_PROXY_SSH_TARGET="$(cat /opt/relaypost/shared/proxy_target)"
export RELAYPOST_ENV_FILE=/opt/relaypost/shared/bot.env
export RELAYPOST_DATA_DIR=/opt/relaypost/data
docker compose --project-name relaypost --file deploy/compose.yml ps
docker compose --project-name relaypost --file deploy/compose.yml logs --tail=50 bot
```

Не выводить compose config без --quiet: env_file может раскрыть секреты.
Не выполнять docker inspect целиком: Config.Env содержит токен.
Использовать точные templates для State/Health, без Env.

## Первичная проверка прямого соединения, 2026-10-20

Docker 29.1.3 и Compose 2.40.3 установлены, image собран, SQLite создана,
repository Secrets и production/tag policy проверены. Initial deploy не прошёл
readiness: с хоста и контейнера api.telegram.org:443 недоступен по таймауту,
как и core.telegram.org; GitHub HTTPS доступен. Проверено несколько адресов
Telegram, OUTPUT ACCEPT, рабочего global IPv6 нет. Кандидат остановлен
deploy-скриптом. Успешный запуск не заявляется.

Владелец указал использовать существующий vpn_proxy_server по схеме planerBot.
Проверка через новый ограниченный SSH-ключ вернула Telegram HTTP 302 с включённой
проверкой TLS. Результат direct-проверки выше не относится к новому пути через Squid.
