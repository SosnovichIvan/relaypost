# Backend reliability and security

Читайте эту справку при изменении auth, persistence, background processing или
операционного поведения сервиса.

## Persistence и конкурентность

- Транзакционная граница соответствует одному бизнес-инварианту.
- Повтор команды не создаёт дубли, если transport допускает retry.
- Unique constraints и conditional updates защищают инварианты также на уровне
  storage; предварительная проверка сама по себе не устраняет race.
- Workers, tasks, threads, queues и shared caches имеют владельца,
  synchronization/cancellation и путь остановки. Запусти доступную для языка
  проверку races или concurrency defects.
- Миграция имеет детерминированный порядок, совместима с rollout strategy и не
  предполагает мгновенное обновление всех экземпляров без подтверждения.

## Identity и секреты

- Authentication устанавливает проверенную identity; authorization отдельно
  проверяет действие над конкретным ресурсом.
- Identity headers доверяются только после подтверждённой gateway/mTLS boundary.
- Passwords используют специализированный password hash; сравнение токенов и
  подписей не должно создавать очевидный timing leak.
- Refresh/session tokens хранятся в форме, минимизирующей ущерб утечки; raw
  secrets и bearer values не попадают в логи и error messages.
- Конфигурация содержит несекретные defaults; секреты приходят из окружения или
  принятого secret store и проверяются на startup.

## Наблюдаемость и тесты

- Correlation/request ID проходит через downstream calls.
- Metrics имеют bounded labels; user IDs, URLs с IDs и произвольные error text
  не становятся labels.
- Traces не содержат credentials и лишние персональные данные.
- Unit tests покрывают business branches и error mapping в принятом стиле.
- Integration tests проверяют реальные transaction/constraint semantics.
- Transport tests проверяют status/code, response envelope и malformed input.
- Contract/generation check предотвращает drift между schema и реализацией.
