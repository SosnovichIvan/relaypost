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
