# GitHub Actions: gates и безопасный PR

Используй эту reference только для репозиториев на GitHub Actions. Механизмы
сверяй с актуальной официальной документацией и существующей конфигурацией.

## Триггеры и DAG

- PR validation запускается на `pull_request` для защищённых целевых веток,
  при новом PR и изменении head; события, меняющие policy-входы, учитываются
  отдельно. При включённой merge queue нужен соответствующий `merge_group`.
- Не ставь workflow-level `paths` filter на единственный required check:
  пропущенный workflow может оставить ожидаемый check pending. Для маленького
  проекта проще всегда проверять весь набор. Для большого — detector + jobs
  по влиянию + стабильный обязательный aggregate gate.
- Aggregate с `needs` должен выполняться даже при failed/cancelled dependencies
  (`if: ${{ always() }}`), а не пропускаться. Он сверяет результаты **всех**
  требуемых jobs со scope detector: success необходим для нужного job;
  skipped допустим только для явно невыбранного. failure/cancelled/unknown,
  отсутствующий/невалидный scope и провал detector → failure. Не проверяй
  только отсутствие строки `failure`: это пропустит cancelled/skipped.
- При matrix проверь failure handling каждой выбранной комбинации. Используй
  уникальные стабильные check names; не переименовывай required checks без
  согласованного перехода protection. Не добавляй `continue-on-error`/`|| true`
  к обязательным тестам. Flaky retry не скрывает итоговый провал; бюджет явный.

## Среда и артефакты

Закрепи runtime и зависимости по lockfiles, установку делай воспроизводимой.
Версии third-party actions фиксируй по принятой supply-chain policy; при новой
настройке предпочитай проверенный полный commit SHA с читаемой версией рядом.
Cache key учитывает OS/runtime/lockfile; cache не заменяет install/verify.
Задай timeout, healthcheck и readiness для test services; фиксированный sleep
не доказывает готовность. Логи, JUnit/coverage и failure screenshots сохраняй
с ограниченной retention, без пользовательских payload/secrets.

## Trust boundary

Минимальные permissions, обычно `contents: read`; checkout без сохранения
credentials, если push не нужен. Не выполняй код fork PR через
`pull_request_target` с привилегиями. Metadata PR недоверенна: не вставляй
title/branch/body напрямую в shell expression; передавай как quoted env/data.
Не запускай недоверенный PR на runner с доступом к production или на VPS.
PR не использует production secrets, не отправляет настоящие сообщения и не
делает publish/deploy. Не потребляй артефакт недоверенного run в привилегированном
workflow как исполняемый код.

## Enforcement

Workflow и branch protection — разные сущности, обе входят в настройку gates.
После проверки DAG выпиши точные required checks (либо один проверенный
aggregate), прочитай protection целевых веток и rulesets, сохрани исходное
состояние для сравнения. При наличии разрешения на этот репозиторий примени
точечное изменение, сохраняя прежние checks и привязки к доверенному App.
Не заменяй всю защиту урезанным шаблоном. После записи перечитай каждую ветку
и сравни checks, strict mode, admin enforcement, PR/reviews и запреты
force push/удаления; сверь исключения reviewer ruleset отдельно.
Синхронизируй декларативную конфигурацию репозитория через обычный PR.
Если доступ или разрешение отсутствуют — запроси их и пометь enforcement как
`not_verified` и незавершённую часть задачи, не считай JSON применённой защитой.
Reviewer bypass не должен обходить quality checks. Не переноси личный
GitHub actor ID, VPS, токены или исключения из другого проекта.

Официальные источники (проверены 2026-10-20):

- [Workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax): paths, needs, if, permissions, services.
- [Secure use](https://docs.github.com/en/actions/reference/security/secure-use): недоверенные входы и supply-chain.
