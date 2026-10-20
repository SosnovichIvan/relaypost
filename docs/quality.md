# Quality contract

Один сервис: `relaypost/`, Python. Bot, gateway, domain, storage и runtime
проверяются одной suite. UI N/A: взаимодействие через Telegram команды/кнопки.
Браузер и Cypress N/A. DB: stdlib SQLite фактической версии runner, fixtures
в изолированных временных каталогах. Gateway: httpx MockTransport, только
фиктивные токены и ID; live calls запрещены test harness.

`make check`: ruff format/check, mypy, pytest с coverage всех файлов relaypost,
отдельные пороги lines >=90% и branches >=90%. Исключений авторского кода нет.
Бот contract/integration: настоящий dispatcher → service → SQLite → HTTP stub.
Проверки схемы: empty create, repeated upgrade populated DB, foreign keys,
UNIQUE, rollback, crash recovery. CI job Backend, обязательный gate Quality gate.
Branch policy: feature → develop; develop → main. Ревью обязательно, bypass нет.
Remote enforcement необходимо проверить отдельно после появления GitHub checks.
Нагрузка и реальный доступ к площадкам stub-тестами не подтверждаются.

Проверены негативные пороги coverage и направления PR. Первый удалённый run
39384498521 упал на несовместимом Python-backport в lock и Quality gate также
упал: failure компонента не скрывается. Lock исправлен environment marker для
Python <3.11. Docker verify build выполнен локально на Python 3.12 / Linux ARM64.
Remote enforcement: **verified**, 2026-10-20. Обе ветки перечитаны после PUT:
strict Quality gate от GitHub Actions app 15368, enforce_admins=true, review=1,
last-push approval, dismiss stale reviews, conversations resolved,
force-push/delete=false, bypass отсутствует. Декларация: `.github/branch-protection.json`.
Реальный run [39384653090](https://github.com/SosnovichIvan/relaypost/actions/runs/39384653090)
прошёл Backend и Quality gate на af56b2d. Прямой push в защищённые ветки
не выполнялся; запрет подтверждён конфигурацией GitHub API, а не такой попыткой.

Финальная suite: 107 tests, 100% lines, 98.84% branches на Python 3.9.6
(coverage 7.10.7); GitHub runner Python 3.12.15 прошёл те же gates.
Docker build, запуск non-root с SQLite migration и `docker compose config --quiet`
проверены локально без реальных платформ. Контейнер также проверяется Backend CI.
Релизы N/A: release workflow отсутствует, CI ничего не публикует.
Живая доставка остаётся **gap до подключения тестовых аккаунтов**, проверка
производительности **gap до определения нагрузки**; stub suite их не заменяет.

## Deployment change

Runtime readiness входит в общую suite relaypost/ и пороги покрытия.
CI дополнительно проверяет shell syntax deploy/deploy.sh, приватное формирование
env, отказ на неправильном tag/ref и коммите вне main. Production workflow
самостоятельно повторяет make check до доступа к секретам; PR workflow
не имеет доступа к VPS. Compose не публикует HTTP ports.
Live VPS deployment/readiness проверяется отдельно в рамках разрешения владельца.
Автоматический rollback и restore в production не тестируются разрушительными
экспериментами; для них используется изолированный deployment harness.

Deployment follow-up: 128 tests passed locally, coverage 100% lines / 98.85%
branches; actionlint 1.7.12 accepts both workflows; production Compose config
validates with --quiet. The real initial deploy built the image but correctly
failed its readiness gate and stopped the candidate because Telegram HTTPS
was unreachable from the VPS. Network diagnostics and current limitations are
recorded in docs/deployment.md; this is not a passed live delivery check.

## Closed Telegram tunnel

Новый runtime network module входит в общий 90% lines/branches gate.
Infra component telegram-proxy: Dockerfile target=tunnel, Compose secrets без
published ports, image build в Backend job, SSH+TLS preflight в deploy до остановки
writers. Unit tests проверяют Telegram-only routing для API/media, запрет fallback,
закрытие clients и sanitized errors. Deployment harness включает preflight failure.
Приватные SSH keys не нужны PR tests и не попадают в image/build context.

Live proxy acceptance completed on 2026-10-20 at commit 6acf0c4:
bot+tunnel healthy / zero restarts, actual getMe via the mounted Telegram
transport, fresh getUpdates heartbeat, SQLite integrity ok. 152 local tests,
100% lines / 98.91% branches overall; network module 100% lines/branches.
GitHub run 39390128217 passed both required checks. No live publication was sent.
