# Чистый деплой без падений

Этот архив уже проверен:
- все `.py` файлы компилируются;
- `config.py` содержит `get_settings`;
- `dialog.py` содержит `handle_message`;
- `language_guard.py` содержит `detect_language`;
- `requirements.txt` не содержит Python-кода.

Что делать:
1. Создай новый пустой GitHub repo.
2. Загрузи СОДЕРЖИМОЕ папки `neuro-balance-bot-main` из этого архива.
3. Не вставляй тексты фиксов в `.py` файлы.
4. Railway Start Command:
   `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. После deploy открыть `/health`.
