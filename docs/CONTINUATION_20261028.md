# Продолжение стабилизации NeuroBalance: 28.10.2026

Работа продолжена от PR112/main `2d2ab0c50aefda0b9cfb93b60ed79e255189d076`, Railway deployment `82c55e42-c0e1-4750-b0dc-fe391e0e58af` (SUCCESS). Репозиторий, сервис, база, объём `/data` и единственный экземпляр сохранены. Нового проекта, тестовых медицинских карточек, экспериментальных сообщений пациентам нет.

## Подтверждённые исходные факты продолжения

- Повторная Railway-проверка: один сервис/replica, нет pending deployment; main соответствует живому release. Предыдущий release доступен для rollback.
- Production Monitor run [40619402805](https://github.com/vrempel90-dev/neuro-balance-bot-main/actions/runs/40619402805), 28.10 07:10 Asia/Almaty: health OK, worker работает, pending/processing/uncertain = 0; done16, failed3. Это **failure**, не успешный мониторинг.
- В 05:05:52 Asia/Almaty CRM patient-lookup ответила HTTP429 на четыре формата номера. Зафиксированы три безопасных отказа CRM; недоступность не превращала клиента в нового. Исходный запрос/ответ не воспроизводился. Failed-задания не удалялись и не объявлялись обработанными.
- Ограниченная выборка20 production `bot_decision` после первого release:13 NEW/send=true;3 NEW/empty_text/send=false;3 CRM_UNAVAILABLE/send=false;1 ACTIVE_BOOKING/send=false. Нарушений допуска среди этих20 событий не обнаружено. Это не все пациенты и не доказательство доставки.
- В восьмичасовой Railway-выборке481 samples: CPU max0.0092503, memory max0.13572096GB, disk max0.04657152GB. Эти метрики не доказывают отсутствие будущих утечек.
- Подключённого CRM source/test-tenant/test-number не обнаружено; CRM UI требует входа. В доступных репозиториях нет исходников Vercel CRM proxy. Wazzup callback направлен в действующий CRM gateway; его нельзя обходить прямым переключением на бота.

## Исправления в этом продолжении

| Дефект / недостаток | Изменение | Проверка до release |
|---|---|---|
| CRM429 приводила к немедленному отказу; варианты номера создавали всплеск запросов | Общий лимит2 одновременных lookup GET; не более2 попыток; Retry-After соблюдается, длинное/неопределённое ожидание закрывает допуск. Booking POST не повторяется |26 новых transport/concurrency cases; реальные POST отсутствуют |
| Voice/photo/PDF игнорировались входящим обработчиком | Wazzup v3 type/contentUri нормализуются; CRM admission до ASR/download/reply; один ASR с auto RU/KK; для непрочитанного вложения правдивый запрос текста | Реальный ASGI/SQLite pipeline, mocked CRM/ASR/channel; дополнительно предусмотрен synthetic TTS→ASR live probe |
| Модель могла заявить, что прочла captioned attachment | Флаг unread на текущий ход и финальная RU/KK защита; следующий текстовый ход сбрасывает флаг |4 RU/KK claim cases, сброс и legacy audio URL admission |
| Монитор мог быть зелёным при незащищённом webhook | Отдельные operational/security statuses; CRM forwarding enforcement только после реального совпадения текущего секрета, свежего доказательства и точного callback origin/path | Все aliases, свежесть/rotation/restart; автоматический обход CRM gateway запрещён |
| Внешний cron не доказывал частоту/состояние между запусками | В том же процессе local60s/dependencies300s, bounded persisted observations14d; реальные внешние gaps отражаются отдельно | Worker lifecycle/freshness/failures tests; runtime результат проверяется после deploy |
| Model probe проверял только непустые приветствия | Действующий prompt/parser,4 synthetic turns RU→KK/KK→RU, контекст/возраст/роль; отдельный TTS→ASR probe | Контракты/тайм-ауты/отсутствие PHI, реальные провайдеры — только после deploy |
| Report24h был rolling snapshot | Завершившийся09:00–09:00 Asia/Almaty, верхняя граница исключается, дубли durable ingress включены отдельно | Boundary/cap/privacy tests |
| Первый timeout во время rollout прерывал monitor | Ограниченный readiness polling переживает первоначальный timeout; ожидает конкретный release SHA | Изолированная проверка recovery; live rollout проверяется отдельно |
| Старый manual repair workflow содержал ошибки YAML/bash и ссылки на удалённые тесты | Исправлены синтаксис/актуальный pytest и проверка реально существующего gateway до действий | YAML parsing/bash/contract checks; ручной repair не запускался |

## Реально выполненные автоматические проверки

Полный локальный pytest: **917 passed, failures0, errors0, skipped0**,24.303s (JUnit). Компиляция и `pip check` успешны. Зависимости приложения не менялись относительно PR112; отдельный dependency audit повторяется в CI. Новые тесты не вызывают внешнюю CRM/OpenAI/Wazzup и не заменяют live-проверки.

Существующая матрица30 обязательных сценариев находится в `STABILIZATION_SCENARIOS.md`. Её исторические результаты753/722 сохранены как исторические; новый полный прогон917 включает прежние регрессии и новые media/auth/backoff/monitor cases. Интерпретация изображений/PDF не реализована и не заявляется: пациенту предлагается изложить запрос текстом.

## Приёмка и границы доказательств

| Этап | Проверенное до этого release | Что остаётся подтвердить |
|---|---|---|
|1 Аудит | Production source/SHA/deployment, архитектура, snapshot, ошибки и rollback | CRM proxy source недоступен |
|2 Новые клиенты | Fail-closed regression, реальный existing lookup предыдущего release, ACTIVE_BOOKING blocked в выборке | Новый live lookup/полный разрешённый канал |
|3 RU/KK | Детерминированные language/context tests |4 real-model turns и ASR после deploy; качество вне сценариев не доказано |
|4 Запись | Verified readback/idempotency/slot races в тестах; реальные доступные слоты предыдущего release | Реальные directory/readback probes; новый CREATE только в разрешённом test tenant |
|5 Роль | Нет неподтверждённого handoff, regression tests | Real-model role checks; доставка реальной эскалации не доказана |
|6 Технические ошибки | Durable FIFO/dedupe/restart/send/booking safety; найден429 и добавлен bounded lookup recovery | Live webhook auth contract и исторические3 failed jobs требуют видимого учёта |
|7 Автотесты |917 passed; mocks явно обозначены | CI этого immutable revision и live probes |
|8 Railway | Один экземпляр, существующий volume, SUCCESS предыдущего release, rollback доступен | Terminal SUCCESS нового release, health/storage/backup/worker |
|9 Мониторинг | Внешние runs/artifacts существуют; failure действительно выявлен | Фактическая периодичность после deploy и доставка уведомления человеку |
|10 Отчёт09:00 | Существующая automation ID6ae0de6b5d9c8191ab88ec0193b79939 enabled, explicit TZID Asia/Almaty, тот же ChatGPT канал, дубликата нет | Recorded run/result/delivery; null history не означает ни успеха, ни отсутствия исполнения |
|11 Production E2E | Реальные read-only integration checks предыдущего release | Новый сквозной CREATE/канальная доставка заблокированы отсутствием разрешённого test tenant/number |

## Release / rollback

Мержить только после зелёного полного CI и сверки неизменного main. Дождаться фактического Railway SUCCESS и совпадения `/health.release_sha`; затем читать отдельные результаты CRM, model, ASR, security и monitor. Общий FAILED не скрывать частичными успешными probes.

Для rollback этого продолжения использовать проверенный PR112 deployment `82c55e42-c0e1-4750-b0dc-fe391e0e58af`; медицинскую SQLite автоматически не откатывать. Новые таблицы additive. Startup создаёт проверенный SQLite checkpoint; факт production checkpoint подтверждается отдельно логом после deploy. Исходящие/booking uncertain claims не стирать и не переигрывать. Возврат к PR112 вернёт его ограничения media/monitor/backoff; это аварийный откат, а не устранение их причин.
