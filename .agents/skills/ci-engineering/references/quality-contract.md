# Контракт PR-проверок

## Матрица проекта

Для каждого собственного приложения/сервиса/общей библиотеки запиши:

| Поле | Что фиксировать |
| --- | --- |
| Компонент | ID, root, runtime entrypoints и потребители |
| Проверки | Команда, working directory, версия инструмента, ожидаемый exit code |
| Зависимости | Целевой движок/версия, healthcheck, timeout, изоляция и cleanup |
| Тесты | Unit, integration, contract, E2E — что именно подтверждают |
| Coverage | Scope всех авторских файлов, lines/branches, пороги, exclusions, путь отчёта |
| CI | Job/check name, триггер, связь с required gate |
| Статус | implemented/verified/gap/N/A и доказательство либо причина |

Формат может быть Markdown или существующая машинная конфигурация проекта.
Не вводи второй источник команд рядом с рабочим Makefile/package scripts.

## Подбор по фактическим компонентам

- Backend/API/worker/bot/CLI: format/lint, типы или эквивалент static analysis,
  unit и integration tests; фоновые задачи проверяются по своим entrypoints,
  даже если используют тот же image. Не требуй отдельного job для каждого
  процесса: общая suite допустима при явном покрытии всех процессов.
- Frontend: lint, types, unit/component и критические пользовательские E2E,
  responsive/keyboard при соответствующем UI. Compile-for-verification не
  заменяет тесты. E2E со stub API не доказывает backend/DB-интеграцию.
- При утверждённой общей delivery policy frontend E2E и bot integration tests
  обязательны: они входят в required checks или обязательный aggregate gate.
  Для нового web frontend выбран Cypress; существующий E2E framework не меняй
  без согласования. Для native UI используй соответствующий UI harness.
  При связи frontend с ботом дополни browser mocks реальным тестовым backend;
  подробности — [bot-integration.md](bot-integration.md).
- БД: целевой движок с healthcheck, пустая схема, upgrade с предыдущей схемы
  с данными, constraints и drift. Downgrade проверяй только если проект обещает
  его поддержку, строго в одноразовой БД; восстановление нельзя доказывать
  удалением production-данных. Для DDL не выдумывай процент SQL coverage.
- Очередь/cache/storage: собственный клиент/worker integration, повтор,
  конкурентность и отказ по значимости сценария. Реальный локальный контейнер
  нужен там, где mocks не подтверждают протокол/транзакционные гарантии.
- Контракты: генерация schema/client из источника истины с diff-check;
  совместимость потребителей при изменении API/events/shared package.
- Infra: shell/config validators, container dependency check, изолированный
  startup/health smoke там, где есть поддерживаемый harness. Terraform plan
  и аналогичные операции учитывают доступ к state/credentials; не запускай
  их с production authority из недоверенного PR.

Один сервис на Go не требует Node, браузер или PostgreSQL без причины.
Несколько независимых сервисов не могут скрыться за общим зелёным lint job.
90% применяется только если это утверждённая policy; при statements-only
инструменте метрика branches остаётся непроверенной до согласованного решения.

## Полнота и влияние

При добавлении сервиса одновременно обновляй инвентарь и CI. При удалении —
потребителей и protection contexts. При изменении API — producer и consumers;
shared/lockfile/toolchain — все зависимые roots; migrations — сервисы чтения/
записи и rollout tests. Для неизвестного пути выбери полный набор или явно
заблокируй scope, а не тихий пропуск. Не вычисляй «не затронут» только по
названию директории без проверки shared dependencies.

## Примеры приёмки skill, не универсальные команды

1. Python API + worker + React + PostgreSQL: обе runtime-роли видны в матрице;
   backend с реальной БД и миграциями, frontend coverage и E2E, schema diff.
2. Go CLI без БД/UI: gofmt/vet/tests и поддерживаемая метрика покрытия;
   browser/DB — N/A, не фиктивные jobs, branches не названы измеренными.
3. Monorepo с общей библиотекой: её правка запускает проверки всех потребителей;
   ошибка detector или отменённый нужный job проваливает aggregate gate.
4. Docs-only PR: required gate появляется и завершается согласно явной policy;
   workflow-level paths filter не оставляет обязательный check навечно pending.
5. Новый сервис без тестов: зафиксирован gap, не «всё проверено» и не зелёный
   placeholder. Существующий пробел не разрешает молча ослаблять policy.
6. Frontend + bot: отдельно видны browser E2E и bot integration tests, сквозной
   UI → backend → gateway stub путь и их обязательные gates. Browser mocks
   не засчитываются за этот путь; удаление одного gate выявляется как gap.
7. Bot без frontend: обязателен bot integration gate, browser — N/A. Только
   предполагаемая будущая интеграция не запускает бессмысленную live-рассылку.
