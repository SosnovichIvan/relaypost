# Quality contract

Один сервис: `relaypost/`, Python. Bot, gateway, domain, storage и runtime
проверяются одной suite. UI: Telegram команды/кнопки и серверные HTML-формы входа.
Cypress обязателен для web-сценариев; отдельный Login UI E2E входит в Quality gate.
Авторского frontend JS нет: Python renderer входит в backend coverage, HTML/CSS
проверяются браузером, отдельный JS coverage denominator пуст и N/A. DB: stdlib SQLite фактической версии runner, fixtures
в изолированных временных каталогах. Gateway: httpx MockTransport, только
фиктивные токены и ID; live calls запрещены test harness.

`make check`: ruff format/check, mypy, pytest с coverage всех файлов relaypost,
отдельные пороги lines >=90% и branches >=90%. Исключений авторского кода нет.
Бот contract/integration: настоящий dispatcher → service → SQLite → HTTP stub.
Проверки схемы: empty create, repeated upgrade populated DB, foreign keys,
UNIQUE, rollback, crash recovery. CI job Backend, обязательный gate Quality gate.
Branch policy: feature → develop; develop → main. Reviews follow the owner exception below.
Remote enforcement необходимо проверить отдельно после появления GitHub checks.
Нагрузка и реальный доступ к площадкам stub-тестами не подтверждаются.

Проверены негативные пороги coverage и направления PR. Первый удалённый run
39384498521 упал на несовместимом Python-backport в lock и Quality gate также
упал: failure компонента не скрывается. Lock исправлен environment marker для
Python <3.11. Docker verify build выполнен локально на Python 3.12 / Linux ARM64.
Remote enforcement: **verified**, 2026-10-20. Обе ветки перечитаны после PUT:
strict Quality gate от GitHub Actions app 15368, enforce_admins=true, review=1,
last-push approval, dismiss stale reviews, conversations resolved,
force-push/delete=false; initially no bypass. Current owner exception is documented below.
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
не имеет доступа к VPS. Приложение входа имеет только loopback HTTP port;
внешний доступ разрешён через отдельный HTTPS reverse proxy.
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

## Owner merge exception

Explicit owner instruction, 2026-10-20: SosnovichIvan can merge their own and other
contributors' PRs without another person's approval. GitHub rejected classic
per-user allowances on this personal repository (422: organization repositories
only). Reviews now use active ruleset 25461411 for main and develop, declared in
`.github/review-ruleset.json`. Admin RepositoryRole 5 has bypass_mode=pull_request;
SosnovichIvan is the only current admin. Reassess this exception if admins change.
Other contributors still require one approval, stale review dismissal and approval
of the latest push by another person.

Classic protection remains separate: strict Quality gate from Actions app 15368,
enforce_admins=true, conversation resolution and force-push/deletion bans.
`.github/branch-protection.json` sets required_pull_request_reviews=null because
the ruleset owns reviews. Apply both declarations together. Both branches and the
ruleset were read back after applying through GitHub API.

The owner must still use a PR ([GitHub ruleset bypass documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)).
Verified in the owner's PR #1 UI: all checks passed and the checkbox
"Merge without waiting for requirements to be met (bypass rules)" is available.
The generic Review required status can remain until this option is selected.
CI is outside the bypass ruleset and remains mandatory. No PR was merged.

## Personal Telegram and multiple destinations

New paths enter the same Backend / Quality gate, without production credentials.
Tests use real SQLite and a local HTTP server; Telegram Bot API remains behind
MockTransport and Telethon connect is explicitly forbidden by the test harness.
The personal client fixture exercises real TL request types, random_id stability,
rights, story privacy, identity, limits, timeout/RPC failure and logout ordering.

HTTP integration covers phone/code/2FA and QR, CSRF/Origin, browser binding, expiry,
attempt budgets, payment-required rejection, explicit save, DB failure, reauth,
cancel and encrypted session storage. Migration coverage starts from populated
v1, preserves deliveries and backfills peer guards. Legacy credentials are empty
placeholders; tests prove no personal session reaches an old adapter, no outbound
fallback occurs, and legacy deletion cannot erase a session. Disconnect racing
with reconnect preserves the new unrevoked credential.

Selection tests cover multiple accounts/channels, shared-peer account choice,
pagination, owner isolation, 20-place limit, immutable confirmation, stale clicks,
partial failure and duplicate protection. Polling continues during a batch; task
errors are sanitized and background work is cancelled/joined on shutdown.

Local checks: ruff format/check, strict mypy, complete coverage suite; production
image builds on Python 3.12 / Linux ARM64 and imports crypto/login/storage under
UID 10001, read-only root and no network. actionlint accepts workflow changes.
The private Squid upstream CONNECT to a Telegram DC on 443 was verified without
login or posts. Live auth, TLS-domain acceptance and actual publications remain
unverified until app credentials, domain and designated accounts are supplied.

Final local acceptance: 230 tests, 100% lines / 99.05% branches across relaypost/;
no source exclusions or reduced thresholds. Login pages, selection and storage
modules have 100% line/branch coverage. Live platform behavior is not inferred
from these stub tests.

Browser harness lives only in tests/e2e_backend.py and web-tests/; it is excluded
from the production image/archive. It uses a generated local CA, real TLS, native
forms, actual CSRF/cookies and SQLite; only external Telegram client/network is
stubbed. The CA is explicitly trusted by the runner, not by disabling certificate
verification. Native form POST requires strict-origin rather than no-referrer:
Chromium otherwise sends Origin:null. The login path never enters Referer.
The end-to-end test saves a personal session via the UI, publishes through the
real bot dispatcher/service/adapter to two controlled peers, then verifies two
stored deliveries and two outbound TL writes despite repeated confirmation.
QR/cancel, bad codes, browser binding and expired links are also browser scenarios.
Both Backend and Login UI E2E must succeed in the stable aggregate; tag verification
repeats browser acceptance before any production secrets are exposed.
