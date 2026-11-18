# Neuro Balance 2: подготовка к развёртыванию и откат

Production deployment отдельно не разрешён и этой работой не выполняется.

## Конфигурация новой версии
Существующие CRM/Wazzup API URL, webhook aliases и переменные авторизации сохранены. API CRM не расширялся несуществующими endpoints. База — существующий SQLite на /data, один процесс Uvicorn и одна replica. Точка входа live_main:app. График AI остаётся 20:00–08:00 Asia/Almaty.

Перед разрешённым deployment:
1. Оставить GitHub autodeploy выключенным, независимо проверить serviceInstanceAutoDeployStatus.enabled=false. Обычный push не является разрешением deployment.
2. Сделать проверенную приватную резервную копию базы; не печатать её содержимое. Сохранить deployment ID и предыдущий SHA.
3. Сверить OPENAI_API_KEY, CRM_BOT_SECRET/EXTERNAL_BOOKING_API_SECRET, WAZZUP_API_KEY и channel IDs без вывода значений. Проверить учётные записи и условия обработки данных клиники.
4. OPENAI_VOICE_MODEL=gpt-transcribe рекомендован после синтетических RU/KK/mixed/noise/Opus проверок. Явно заданный whisper-1 по-прежнему поддерживается; переменные действующего production не изменены. Настройки языков нового ASR передаются как ru+kk, без принудительного русского. Модель и её наличие в аккаунте проверены через API.
5. AI_BRAIN_MODEL=gpt-5.6-terra, OPENAI_MODEL/AI_HUMANIZE_MODEL=gpt-5.6-luna остаются совместимыми. Модели проверены по актуальному каталогу и доступу аккаунта; менять production автоматически не требуется.
6. MEDICAL_DOCUMENT_PROCESSING_ENABLED по умолчанию false. Включение для настоящих медицинских документов требует основания обработки, проверки условий провайдера и решения клиники. Синтетические документы тестировались в изолированном дочернем процессе с true.
7. OPENAI_DOCUMENT_MODEL=gpt-4o-mini использует строгую JSON-схему с kind и строкой text. Поддерживаются JPG/PNG/WEBP/PDF; ограничены размер, пиксели, число страниц и вложений. Нечитаемый документ/снимок без заключения не превращается в диагноз. DICOM не заявляется.
8. MEDICAL_HISTORY_RETENTION_DAYS=30: удаляется история только старых завершённых обращений. Неопределённые записи, незавершённый inbox, ownership и идемпотентность сохраняются. Рабочие файлы медиа не сохраняются модулем документов. Retention запускается hourly worker, не отдельным production-действием в ходе разработки.
9. Клиника должна согласовать казахские медицинские формулировки; автоматические тесты проверяют структуру и ограничения, не заменяют клиническое согласование и юридическую оценку требований Казахстана.

## CRM provenance: обязательная граница
Поддержка aiSessionId не подтверждена. См. CRM_SESSION_PROVENANCE_CONTRACT.md. Без проверяемого идентификатора происхождения карточки переход NEW -> существующая карточка не предоставляет AI нового допуска: событие сохраняется и передаётся сотруднику. Номер, время, статус NEW и старый локальный lead_id недостаточны. Нельзя включать обход ради успешного сценария.

## Миграции и совместимость
state.init_db() и production_inbox.init_db() идемпотентны. Таблицы provider_deliveries/provider_receipts добавляются идемпотентно при первом использовании delivery API/monitor. Существующие таблицы не удаляются. Новые handoff статусы используют существующее TEXT-поле inbox. Legacy outgoing confirmed означает принятие API, а DELIVERED появляется только после настоящего callback. Новые receipt и handoff ошибки видны в мониторинге. Неопределённые операции не повторяются автоматически.

## Проверка после отдельного разрешения запуска
- Проверить volume /data, SQLite quick_check, живой worker, фактический SHA и защиту operational endpoints.
- Согласованно настроить webhook authentication по существующему reconcile workflow; сначала подтверждение Wazzup, затем локальный guard. Не менять секреты вслепую.
- Проверять только специальные тестовые контакты/стенд. До этого проверка /channels и /doctors доказывает лишь read-only доступ, а не доставку/запись.
- Проверить реальные delivery callbacks и ручную передачу; не объявлять передачу выполненной до положительного ответа CRM.
- Вручную сверить старые uncertain booking/send claims; не выполнять повторный POST после неопределённого исхода.
- Ручной Production Monitor принимает необязательный expected_release только для уже разрешённого deployment. Push в main сам по себе больше не требует совпадения production SHA. Реальные ошибки runtime по-прежнему оставляют монитор красным.

## Откат
Вернуть предыдущий deployment или исходный SHA через согласованную процедуру; не запускать откат автоматически. Сохранить базу и новые таблицы/claims: старый код их игнорирует, удаление может потерять дедупликацию. Не восстанавливать старую базу поверх новых неопределённых операций без ручной сверки. Autodeploy остаётся выключенным. Backup tag backup/neurobalance-before-2-2026-11-17 указывает исходный 4b8c928e3d3ef369b07debec96cb81f8db49d5d9.

## Источники технических контрактов
- https://wazzup24.com/help/api-en/webhooks/ — statuses callback.
- https://developers.openai.com/api/docs/guides/speech-to-text — multilingual file transcription.
- https://developers.openai.com/api/docs/models/gpt-transcribe — duration-based pricing $0.0045/min.
- https://developers.openai.com/api/docs/models/whisper-1 — legacy $0.006/min.
- https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6 — существующие Terra/Luna.
