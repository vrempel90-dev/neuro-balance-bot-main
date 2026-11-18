# Neuro Balance 2.0: итог разработки — 18 ноября 2026

Доступные этапы разработки и проверок выполнены и опубликованы в main. Полная эксплуатационная приёмка остаётся открытой: production не развёртывался, реальная доставка/запись не выполнялись, поддержка CRM provenance не подтверждена. Уточнение пользователя о безопасной передаче сотруднику реализовано без обхода NEW_LEADS_ONLY.

## 1. Архитектура

Сохранён FastAPI modular monolith, SQLite на /data, один writer/worker и одна replica. Долговременный inbox отделяет приём webhook от последовательной обработки диалога. Единственный разговорный агент использует независимые медицинские и CRM gates; критические ограничения не зависят только от текста модели. Горизонтальное масштабирование SQLite этой проверкой не разрешается.

## 2. Изменения

Основные файлы: admission.py, agent.py, dialog.py, main.py, production_inbox.py, state.py, services.py, crm.py, medical_rules.py, media.py, voice.py, semantic_router.py, data_retention.py, ai_budget.py, production_monitor.py, config.py, env.example, scripts/live_acceptance.py и соответствующие tests/test_neuro2_*.py. Точный список всех файлов доступен через git diff --name-status 4b8c928..HEAD.

Реализованы смысловая маршрутизация с дословным подтверждением, отдельные сведения пациента и отправителя, противопоказания и возраст, безопасная запись, обработка нескольких вложений по порядку, provider receipts, восстановление и удержание неопределённых исходов, ограниченное хранение завершённых медицинских историй. Исправлены существующие workflow и зависимость трёх положительных тестов от текущего времени; production запрет дневной отправки сохранён.

## 3. Что сохранено

Wazzup provider/API/channel configuration, webhook aliases, существующие CRM endpoints, источник утверждённых медицинских правил, график 20:00–08:00 Asia/Almaty и staff takeover. Реальные настройки production не заменялись новыми значениями. Autodeploy отключён; push main не запускает production deployment.

## 4. Голосовые

Реальные запросы OpenAI на синтетических файлах прошли для RU, KK, смешанной речи, шума и OGG/Opus. По умолчанию gpt-transcribe с ru+kk hints; явный whisper-1 совместим. Учитывается фактическая длительность и бюджет. OGG локально закодирован и полностью декодирован FFmpeg. Естественная речь носителей казахского языка требует проверки клиники: TTS не заменяет такую приёмку.

## 5. Документы

Реальная vision-модель проверена на синтетическом PNG-заключении, многостраничном PDF и нечитаемом изображении. Строгая схема требует строковый извлечённый текст. Ограничены размеры, пиксели и страницы. Снимок без заключения не превращается в диагноз. Обработку настоящих медицинских документов нужно отдельно включить после решения клиники; MEDICAL_DOCUMENT_PROCESSING_ENABLED=false по умолчанию. DICOM не заявляется.

## 6. Противопоказания

Канонические восемь пунктов валидируются против утверждённого источника. Отсутствие/повреждение источника закрывает запись. Нужны полный список, принятие сообщения провайдером и последующее явное подтверждение отсутствия противопоказаний для текущего пациента. Документ, вопрос или старый boolean не заменяют подтверждение. Смена пациента сбрасывает clearance. Возраст 16–75, сопровождение 16–18 и экстренная передача покрыты регрессиями.

## 7. Сохранение обращений

Webhook подтверждается после долговременного commit. Дедупликация, FIFO и восстановление не повторяют неопределённые побочные операции. Handoff pending/confirmed и provider accepted/delivered/read различаются; callback-before-response сохраняется, ошибка поздней доставки видна мониторингу. Retention минимизирует только старые завершённые медицинские истории, сохраняя unresolved inbox, ownership и uncertain claims.

## 8. CRM

Read-only GET doctors и check-slots вернули HTTP 200; Wazzup GET channels — HTTP 200 и наличие настроенного канала. Эти результаты подтверждают чтение API. Реальный booking POST и отправка пациенту не выполнялись. Автотесты проверяют положительные/отрицательные контракты расписания, readback и идемпотентность на изолированных ответах.

Номер телефона, время, NEW-статус и локальный lead_id не дают допуска к существующей карточке. Нужна подтверждённая CRM связь с текущей AI-сессией, без staff/history/booking/closed veto. Поддержка aiSessionId неизвестна; новый endpoint не выдуман. При неподтверждённом происхождении обращение сохраняется и передаётся сотруднику. Требуемое изменение внешнего API: [CRM_SESSION_PROVENANCE_CONTRACT.md](CRM_SESSION_PROVENANCE_CONTRACT.md).

## 9. Команды и доказательства

Проверенный код: aa6ba87722d8b263505d1c798295f7dfa75f245c.

| Проверка | Команда / evidence | Результат |
|---|---|---|
| Полный Linux CI | python -m pytest -q --junitxml=validation-results.xml | 1131 passed, 1 skipped, 0 failed; exit 0 |
| Компиляция Linux | python -m compileall -q . | exit 0 |
| Аудит production dependencies | python -m pip_audit -r requirements.txt --format=json --output=dependency-audit.json | 0 известных уязвимостей; exit 0 |
| Core booking | команда workflow repair-dashboard-contract.yml | 155 passed; exit 0 |
| Dashboard contract | команда того же workflow | 8 passed; exit 0 |
| Дневной gate и положительные отправки | day-clock-green.log, принудительно дневное время для регрессии | 22 passed; exit 0 |
| Реальные синтетические OpenAI запросы | scripts/live_acceptance.py --live, live_final/report.json | 11/11; exit 0 |
| Дополнительный разговор | live_remaining/report.json | 2/2; OGG повторяется, всего 12 уникальных live случаев |
| Windows полный прогон | final-full-windows.xml, отдельный basetemp | 1123 passed, 7 failed; exit 1 |
| Windows повтор исправленных случаев | final-recheck.xml | 2 passed, 6 failed; exit 1 |

Единственный Linux skip — test_ogg_fixture_is_encoded_and_fully_decoded_with_real_ffmpeg: runner не содержит явно заданный локальный Windows FFmpeg. Локально этот тест прошёл, реальный OGG прошёл live acceptance. Шесть оставшихся Windows failures — проверки POSIX mode 0600 в backup/runtime/webhook-auth; на Linux проходят. Седьмой результат полного Windows прогона относился к старому language-тесту; исправленный параметризованный тест прошёл повторно. Старые красные результаты сохранены и не скрываются.

Linux доказательства: [полный набор и security](https://github.com/vrempel90-dev/neuro-balance-bot-main/actions/runs/44230308483), [booking и dashboard](https://github.com/vrempel90-dev/neuro-balance-bot-main/actions/runs/44230308375). Локальный pip check, compile и git diff --check также прошли. Независимые code reviews выполнены; обнаруженные urgency, retention и семантические ошибки исправлены с RED/GREEN регрессиями.

Синтетический live harness запускается отдельным процессом со свежей SQLite, блокирует CRM/Wazzup transport и ограничивает OpenAI вызовы/бюджет. live_final: 14 API calls, conservative reserved USD 0.923136; live_remaining: 3 calls, USD 0.280548. Это лимиты резервирования соответствующих прогонов, не итоговый счёт и не суммарная стоимость всех попыток. Промежуточные неуспешные попытки также сохранены.

Локальные доказательства находятся вне git: ../../outputs/neurobalance_2_completion_2026-11-18. Матрица всех 60 сценариев с границами mocks/live: [NEURO2_ACCEPTANCE_MATRIX_RU.md](NEURO2_ACCEPTANCE_MATRIX_RU.md). Общий quality_gate.py/quality-gates.json в проекте не настроен; использованы реальные CI-команды проекта.

## 10. GitHub

Исходный SHA: 4b8c928e3d3ef369b07debec96cb81f8db49d5d9. Опубликованы резервные метки backup/neurobalance-before-2-2026-11-17 и backup/neurobalance-2-checkpoint-2026-11-18 (a96e8d3).

Новые программные коммиты: fd01f23, a96e8d3, 990f08d, aa6ba87. Финальный отчёт и отметка завершения плана сохраняются следующим documentation commit; его SHA фиксируется в delivery-manifest.json вне git, чтобы не создавать самоссылку в коммите. Работа только в [main](https://github.com/vrempel90-dev/neuro-balance-bot-main/tree/main), без feature-ветки/PR/force-push. Резервный Git bundle включает main и обе метки.

## 11. Railway

[Инструкция deployment, переменные, миграции и rollback](NEURO2_DEPLOYMENT_RUNBOOK_RU.md) подготовлена. Production не развёртывался. Сохранён deployment 1a4d7d51-ca7c-4f0d-b03f-aad66d6d349f с исходным SHA 4b8c928. Перед push отдельно проверялся enabled=false для GitHub autodeploy.

Read-only диагностика действующей старой версии обнаружила 6 booking claims с uncertain исходом, 292 inbox задания в failed и выключенную webhook authentication. Это не доказательство шести реальных записей или 292 потерянных сообщений. Очередь/claims не очищались и операции не повторялись. Production Monitor сохраняет failure runtime_status_requires_attention; source CI success не означает здоровый production.

## 12. Оставшиеся границы

До эксплуатационной приёмки требуются проверяемый CRM provenance для продолжения после создания карточки, согласованный тестовый контакт/стенд для настоящей доставки и booking roundtrip, сверка старых unresolved операций и согласованная настройка webhook auth. Клиника должна проверить казахские медицинские формулировки и основания обработки медицинских данных. Автоматические и синтетические тесты не заменяют эти действия. Production activation запрещена без отдельного разрешения пользователя.
