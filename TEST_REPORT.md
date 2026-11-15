# Фактические результаты проверок

Дата: 2026-11-15. Python 3.12, зависимости requirements.txt и tests/requirements-dev.txt. Windows venv дополнен tzdata только для тестовой машины; production dependencies/config не менялись.

## Итоговый полный запуск на неизменных исходниках

**986 passed / 6 failed / 0 errors / 0 skipped**, 326.11s, exit 1. Всего 992 проверки; все **217 новых регрессионных/приёмочных проверок прошли**, включая 73 многоходовых диалога. Падения — неизменённые Windows assertions POSIX 0600:

- `tests/test_stabilization_backup.py::test_checkpoint_restores_original_database`
- `tests/test_stabilization_runtime.py::test_storage_is_private_and_uses_wal`
- `tests/test_stabilization_webhook_auth.py::test_single_patch_preserves_path_query_and_all_subscriptions` — четыре webhook paths.

Это **не полностью зелёный Windows suite**. Не было пропуска/подавления тестов или ослабления их проверок. Linux CI на PR проверяет целевую среду отдельно. Его фактические результаты доступны в status checks конкретного PR head; окончательная пользовательская копия отчёта может дополняться ссылками на этот запуск после публикации ветки.

## Среда и исходное состояние

Первый запуск остановился с тремя collection errors из-за отсутствия IANA tzdata в Windows. После установки tzdata в venv получены 605 passed / 170 setup errors из-за недоступного системного pytest temp. Использование `--basetemp` внутри workspace устранило ошибки среды.

Чистый baseline на исходном SHA: **768 passed / 7 failed**, 132.55s. Два WinError32 в checkpoint и пять POSIX-mode expectations. Исправление A16 закрыло SQLite handles; осталось шесть Windows assertions о POSIX 0600 (один backup, один runtime, четыре webhook auth). Эти тесты не отключались и их ожидания не ослаблялись.

## TDD и бизнес-проверки

| Набор | До исправлений | После |
|---|---|---|
| test_full_audit_patient_safety, первые 71 cases | 39 failed / 32 passed; ещё 6 consent cases воспроизведены после исправления полноты fixture | 71 passed |
| Дополнительные safety evidence/age cases | 5 failed / 4 passed | весь расширенный набор 80 passed |
| Raw scope + affirmative choice | 4 failed | включены в общий business run |
| test_full_audit_dialogues | 65 passed / 8 failed (реальные backend defects, не model quality) | вместе с 84 safety cases: **157 passed**, 153.45s |
| test_full_audit_integrations + admission/dialog suites | исходно 8 failed / 14 passed, ещё один history edge failed | 139 passed |
| Старые booking/conversation/schedule/profile contracts | выявлены устаревшие предпосылки и affirmative «на» regression | 191 passed |
| Независимое ревью adversarial cases | 18 failed / 7 passed; guardian 3 failed; legacy slot protection 2 failed; stop/name 4 failed | review + stabilization_dialog: **83 passed**, 51.95s |
| Исполняемый monitor workflow с synthetic HTTP | 1 failed (реальная попытка secure-webhook request в script) | 1 passed |
| Production source/readiness contracts на замороженных исходниках | один промежуточный inspect-source тест пересёк редактирование файла; отдельно перепроверен | 9 passed |

73 named multi-turn dialogues: relatives 11, complaint/negation/unclear 10, guardian 6, age outside limits 4, contraindication evidence 8, RU FAQ 6, selection/refusal 5, CRM booking outcomes 4, doctor requests 3, language persistence/switch 4, patient switches 3, model transport failures 2, full RU/KK lists 2, no/unknown slots 3, KK FAQ 2. В каждом сценарии минимум два входящих хода, реальные dialog/agent/tools/SQLite и контролируемые внешние границы. Assertions проверяют ответы, состояние и CRM side effects; scripted client обнаруживает исчерпание сценария.

## Реальные команды

Из корня репозитория, `PY` — Python из изолированного venv:

```text
PY -m pytest -q --basetemp=../pytest-baseline-audit --junitxml=../baseline-results.xml
PY -m pytest -q tests/test_full_audit_patient_safety.py tests/test_full_audit_dialogues.py --basetemp=../pytest-root-business-green3 --junitxml=../business-green3.xml
PY -m pytest -q tests/test_full_audit_review.py tests/test_stabilization_dialog.py --basetemp=../pytest-review-final --junitxml=../review-final.xml
PY -m pytest -q tests/test_production_readiness.py --basetemp=../pytest-source-contract-current
PY -m pytest -q --basetemp=../pytest-frozen-release --junitxml=../frozen-results.xml
PY -m compileall -q .
PY -m pip check
PY -m pip_audit -r requirements.txt --format=json --output=../dependency-audit.json
git diff --cached --check
```

Compileall, pip check и diff check: exit 0. pip-audit: **34 dependencies / 0 известных уязвимостей**, exit 0. Проверка добавленных строк credential patterns: 0 совпадений. Первичный SYSTEM_PROMPT, config.py, ai.py, main.py, live_main.py побайтово совпадают с базой в staged tree. Отдельные lint/typecheck/quality_gate команды в репозитории не настроены; не заявляются как выполненные.

## Что эти проверки не доказывают

Нет локального OpenAI API key; values Railway скрыты OAuth. Ни одного реального LLM/model-quality прогона не выполнено. CRM и Wazzup в автоматических тестах изолированы: реальных пациентов/записей/сообщений нет. Linux CI выполняется GitHub PR workflows; его актуальный статус следует проверять на конкретном PR head, а не считать Windows mode-bit errors доказательством Linux failure или успеха.

Production исследован только read-only: release SHA, Railway configuration/status/logs, public health, GitHub monitor run и его сохранённый sanitized artifact. Подтверждены O01–O03, но не выполнены исправляющие production действия. Подробности — REMAINING_ISSUES.md.
