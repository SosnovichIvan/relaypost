---
name: backend-engineering
description: "Реализует и изменяет backend-сервисы на Go, Node.js/TypeScript, Python, Java, .NET и PHP: API, бизнес-логику, persistence, безопасность и наблюдаемость. Применять для написания backend-кода и законченных service changes с адаптацией к стеку репозитория."
---

# Backend engineering

Реализуй проверяемое изменение backend-сервиса, сохраняя контракты, ownership данных
и направление зависимостей целевого репозитория. Не навязывай микросервисы,
gRPC, конкретный router, ORM, DI framework или observability stack, если проект
их не использует.

## Перед изменением

1. Прочитай ближайшие agent instructions, service README, архитектуру и команды.
2. Определи language/runtime, modules/workspaces, точки запуска, transport, contract sources,
   migration tool, storage boundaries и тестовый стек.
3. Проследи существующий путь запроса от transport до domain/persistence и найди
   аналогичное поведение. Зафиксируй инварианты и владельца authorization.
4. Если публичный или внутренний контракт меняется, сначала обнови его источник
   истины и выполни принятую генерацию. Generated-файлы вручную не редактируй.

Всегда прочитай [architecture.md](references/common/architecture.md) и
[reliability.md](references/common/reliability.md). Установщик добавляет
выбранные языковые и framework-профили в `references/options/` и перечисляет их
ниже. Они являются частью этого skill и не применяются самостоятельно.

<!-- agent-skills-lab:references:start -->
Установленный профиль. Обязательно прочитай все выбранные references:

- [Python / FastAPI / Django](references/options/python.md) — Sync/async boundaries, schemas, ORM и Python checks.
<!-- agent-skills-lab:references:end -->

В monorepo применяй профиль только к изменяемому сервису. Язык отдельного файла
без manifest/config не доказывает framework. Правила целевого репозитория имеют
приоритет над общими рекомендациями профиля.

## Реализация

Для API evolution, workers, webhooks, retry или cache прочитай
[service-changes.md](references/common/service-changes.md).

- Сохраняй существующую слоистую или hexagonal структуру. Transport парсит и
  отображает протокол; application/domain принимает решения; repository/client
  выполняет I/O.
- Валидируй синтаксис на transport boundary, а бизнес-инварианты — в domain или
  application layer.
- Сохраняй cause ошибки и классифицируй ожидаемые domain failures. Не раскрывай
  внутренние ошибки клиенту.
- Делай multi-step state changes атомарными. Для повторяемых команд учитывай
  идемпотентность, retries и конкурентные запросы.
- Используй параметризованные запросы. Динамические identifiers допускай только
  после строгого allowlist; значения не собирай конкатенацией.
- Authorization проверяй по субъекту, ресурсу и действию в сервисе-владельце.
  Display role или заголовок от недоверенного клиента не является разрешением.
- Секреты получай через принятый secret/config mechanism; не логируй credentials,
  токены, cookies или персональные payload целиком.
- Добавляй логи, traces и metrics в принятых проектом точках, не создавая второй
  observability stack.
- Сохраняй graceful startup/shutdown и не запускай background work без понятного
  ownership, cancellation и обработки ошибок.

## Проверка

Выполни узкие tests, затем доступные format, generation check, build, lint и
language-specific static/concurrency checks в объёме, пропорциональном изменению.
Интеграционные/contract tests используй для БД, сети и transport boundary.

В результате перечисли изменённые контракты и поведение, миграции, выполненные
команды и оставшиеся риски. Не называй изменение атомарным, безопасным или
backward-compatible без соответствующей проверки.

## Профиль правил проекта

Применяй [профильные правила](references/project-rules.md) только при явном согласии
на правила этого скилла в инструкциях проекта. При наличии approved snapshot
используй его; наличие файла само по себе не активирует правила. Они не запускают другие скиллы автоматически.
