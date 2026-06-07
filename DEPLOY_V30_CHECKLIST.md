# v30 deployment checklist

1. Upload all files from this archive to GitHub.
2. Railway variables:
   - OPENAI_API_KEY
   - OPENAI_MODEL=gpt-4o-mini
   - OPENAI_DIALOG_TEMPERATURE=0.25
   - OPENAI_HUMANIZE_TEMPERATURE=0.25
   - OPENAI_MAX_TOKENS=1000
   - CRM_BASE_URL
   - CRM_BOT_SECRET
   - WAZZUP_API_KEY
   - WAZZUP_CHANNEL_ID
   - BOT_ACTIVE_FROM=20
   - BOT_ACTIVE_TO=8
3. Deploy.
4. Run tests:
   python tests/run_dialog_tests.py
5. Check /health.
6. Test 5 live cases:
   - хочу записаться по акции
   - грыжа/протрузия
   - ничего → ничего
   - уже записан / отменить запись
   - адрес/цена/лечение после списка времени

Expected tests result:
RESULT: 47 passed, 0 failed
