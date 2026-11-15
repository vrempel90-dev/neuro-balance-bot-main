# Безопасная проверка, деплой и откат

Это инструкция, а не разрешение выполнить production-действия. В ходе аудита main, деплой, Railway runtime/env и provider configuration не менялись.

## До согласования релиза

1. Проверить PR diff, Linux full-regression и dependency-security. Утвердить медицинские сценарии с владельцем и просмотреть перевод KK. Использовать текущую модель, не менять её незаметно.
2. Подготовить изолированную среду с отдельным SQLite и mocked/test CRM/Wazzup transports. Нельзя направлять сценарии на реальных пациентов. Для настоящей model-quality проверки получить отдельный разрешённый API-доступ и бюджет; ноль real CRM writes и ноль patient messages.
3. Устранить O01–O03 из REMAINING_ISSUES по отдельному согласованию. Уточнить лимиты CRM, проверить webhook authentication через readback, сверить четыре uncertain claim-записи и классифицировать failed inbox jobs. Не очищать историю ошибок ради зелёного мониторинга.
4. Снять согласованный backup Volume/SQLite, выполнить integrity/restore smoke test в отдельном пути; сохранить предыдущий successful deployment `1a4d7d51-ca7c-4f0d-b03f-aad66d6d349f` / SHA `4b8c928e3d3ef369b07debec96cb81f8db49d5d9` как точку отката.
5. Проверить start `uvicorn live_main:app`, один replica/worker, Volume `/data`, фактический SQLITE_PATH внутри Volume, overlap=0. Убедиться, что рабочие часы 20:00–08:00 Asia/Almaty, NEW_LEADS_ONLY и модель не изменены.

## После отдельного разрешения владельца

1. Зафиксировать проверенный PR head и все зелёные CI gates. Согласованно слить/деплоить только этот commit; в текущей задаче эти действия не выполнялись.
2. Не удалять и не пересоздавать таблицы. Новые patient claims используют существующую таблицу; legacy claims проверяются атомарно, автоматически не освобождаются. Patient context version создаётся при смене пациента.
3. После развёртывания проверить public health release_sha и защищённый read-only monitor/status: Volume, SQLite integrity, worker heartbeat, backlog, unresolved claims. Наличие webhook token проверять, не печатать token/полные URL с token.
4. Проверить контролируемый отдельный test lead только после отдельного разрешения на выбранные интеграционные действия. Пациентам нельзя обещать запись до подтверждённого CRM результата/readback.

## Откат

При неверном допуске, повторном POST, потерянном контексте или растущих ошибках согласованно остановить автоматические записи/направить диалоги сотруднику и вернуть предыдущий verified deployment. Не выполнять rollback самостоятельно из этого PR.

**Важное ограничение:** прежний SHA не знает patient-wide claims и исправлений. Возврат к нему без ограничения записи может вновь допустить повторный POST на другое время. До отката сверить новые pending/uncertain/confirmed claims; сохранить базу и ограничения для affected patients. Не восстанавливать старый backup поверх текущего Volume без сверки: так можно потерять созданные после snapshot записи и idempotency evidence.

После отката повторить health/monitor и read-only CRM reconciliation. Зафиксировать фактический release SHA, время, подтверждённые/неопределённые операции и причину rollback без patient PII/секретов в публичных логах.
