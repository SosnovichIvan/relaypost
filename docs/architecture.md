# ADR 001: RelayPost

Статус: initial implementation decision, 2026-10-20. Нагрузка и SLA пока не заданы.

Сценарий: пользователь открывает управляющего Telegram-бота, подключает несколько
площадок, отправляет контент, видит доступные назначения и форматы, подтверждает
каждую публикацию и получает её статус. Подключение платформы не означает доступ
ко всем форматам: capability зависит от API, назначения и разрешений.

Выбран Python, модульный монолит, SQLite с WAL и один процесс polling. Вариант
PostgreSQL + отдельный worker полезен при нескольких репликах; пока добавляет
эксплуатационные зависимости без подтверждённой нагрузки. MTProto-сессии личных
Telegram и неофициальные MAX user APIs не включаются в официальный Bot API adapter.

## Контракты до реализации

- `bot`: принимает приватные Telegram updates; identity берётся только из Telegram.
  update_id дедуплицируется. Команды: /start, /connect, /accounts, /targets,
  /add, /disconnect, /cancel. Текст/фото/видео создают черновик. Callback
  привязан к владельцу черновика и назначению; повтор не создаёт новую доставку.
- `service`: владеет проверкой принадлежности, capability, подтверждением и
  журналом публикаций. Другой пользователь не видит и не использует чужие аккаунты.
- `store`: SQLite, параметризованный SQL, FK, UNIQUE и атомарные claims.
  Сетевой вызов не выполняется внутри DB-транзакции.
- `gateways`: httpx, TLS verification, timeout, без автоматических retries
  неидемпотентных отправок. Неподтверждённый исход сохраняется как unknown;
  повтор разрешается только после ручной сверки. Прочие ошибки sanitized.
- `vault`: Fernet, ключ только из окружения. Credentials не выводятся в ответы,
  ошибки и логи. Команда с токеном удаляется из личного чата до обработки;
  Telegram уже получил сообщение, поэтому рекомендуются выделенные токены.

## Владение данными и запросы

| Таблица | Владелец/потребитель | Инварианты/удаление | Индекс/объём |
| --- | --- | --- | --- |
| accounts | service /connect | owner+platform+external_id unique, encrypted token, cascade targets | owner lookup; фактический рост неизвестен |
| targets | service /add | FK account, unique account+external_id+kind, verified rights | account lookup, cascade |
| drafts | bot content | owner, один объект контента, expires после 24h | PK id; удаление по сроку |
| deliveries | service publish | unique draft+target, persisted state before network | PK and unique; история удаления cascade |
| updates | polling | PK update_id, processing state before effect | PK; чистка после 7 суток |

SQLite schema migration 001: новая пустая БД, `PRAGMA user_version=1`, повтор
без изменений; неподдерживаемая будущая версия блокирует startup. Upgrade
заполненной v1 сохраняет данные. Не предполагается downgrade в общую БД.
Backup: остановить единственный процесс, SQLite backup API, сохранить отдельно
ключ Fernet. Восстановление нужно проверить в staging до production.

## Зависимости и отказы

`runtime → bot → service → store / gateways / vault`; domain не знает polling.
Telegram polling → owner validation → intent commit → provider → result commit.
Crash после intent до result: статус unknown после перезапуска; автоматического
повторного внешнего эффекта нет. Нет обещания exactly-once или атомарной
публикации сразу на нескольких площадках.

Начальная версия проверяется через gateway stub и реальную SQLite. Живой запуск
требует BotFather token и credentials площадок. Права перепроверяются перед
публикацией. Пересмотр архитектуры: несколько реплик, очереди с расписанием,
нагрузка выше измеренной способности SQLite, OAuth-app approvals.

## Уточнения контрактов первой версии

/addstory создаёт отдельное назначение ВК kind=story после проверки story-upload API.
Пост и история одной площадки — разные targets; можно подтвердить оба формата.
MAX фото/видео загружаются на подписанные oneme/okcdn endpoints, ВК — на
подписанные vk/userapi endpoints. Только HTTPS, allowlist host, без redirects.
Фото/видео ВК и истории проходят upload→save→publish; partial upload не
откатывается при отказе следующего шага. Подпись историй ВК запрещена, чтобы
не терять текст молча. Бот не создаёт VK Клипы под видом обычного видео.
/discover проверяет ограниченный набор MAX/ВК destinations, Telegram задаётся вручную.
