# Neuro Balance 2: integration and rollout status (2026-11-18)

Development is on main. This is an implementation checkpoint, not production acceptance.
Original SHA: 4b8c928e3d3ef369b07debec96cb81f8db49d5d9.
Backup tag: backup/neurobalance-before-2-2026-11-17 (local until explicitly pushed).
Production deployment must remain unchanged.

## Changes
- Reused prior patient isolation/age/guardian/CRM idempotency fixes after reproducing the regression (216 existing audit tests passed before the new medical gate).
- Voice language auto-detection, bounded audio validation, provider cleanup.
- Caption + ordered image/PDF/audio ingestion. PDF rasterization is bounded to 8 pages, images to 20M pixels, batches to 8 attachments, media to the existing 25MB limit. Larger inputs require staff review; they are not silently truncated.
- Medical document processing defaults off until the clinic establishes the legal/data-processing basis for its provider. Set MEDICAL_DOCUMENT_PROCESSING_ENABLED=true only after that review. OPENAI_DOCUMENT_MODEL defaults to gpt-4o-mini; real OCR/ASR/LLM quality remains unverified.
- Canonical eight contraindications validated against SYSTEM_PROMPT_rendered.md. Missing or modified source closes automatic booking. Provider-accepted full checklist and subsequent patient-specific explicit absence are required. Legacy boolean clearance no longer grants booking.
- Staff takeover saved before FIFO processing, retained across stale session writes. Final work-time/ownership send check.
- Durable handoff_pending/handoff_confirmed outcomes retain the original request. Monitor exposes aggregate attention counts. Authorized local operator retrieval/resolution never blindly replays requests.

## Safe rollout (requires separate user authorization)
1. Keep Railway autodeploy disabled. Verify the dedicated serviceInstanceAutoDeployStatus before any main push.
2. Retain the current deployment and take a database backup using production_backup through the established authorized operations path. Never expose its contents in logs.
3. Run one replica and one Uvicorn worker, live_main:app, persistent /data volume, SQLITE_PATH=/data/bot.sqlite3, Asia/Almaty 20:00–08:00.
4. Install requirements and apply idempotent state.init_db()/production_inbox.init_db() initialization. Existing tables/data stay in place; added status values use the existing TEXT column.
5. Establish webhook authentication through the existing reconciliation workflow; do not enable a secret before the provider configuration is confirmed.
6. Verify staging with synthetic media/messages, real-provider test credentials and controlled test contacts before enabling CRM writes. Never test against patients.
7. Reconcile uncertain provider/CRM claims manually before allowing those chats to resume.
8. Roll back to the backup SHA/deployment on failure, preserve the database and unresolved claims. A code rollback does not authorize replaying uncertain operations. Do not automatically re-enable GitHub autodeploy.

## Outstanding acceptance work
- Full regression results and fixture migration for the stricter checklist gate; no failed check may be called passing.
- Actual RU/KK/mixed/noisy audio and image/PDF model quality, real provider roundtrip, validated model access/pricing, clinical approval of Kazakh terminology.
- CRM provenance for initially absent contacts that become in-progress leads. Timing alone is insufficient proof; until the CRM supplies trusted session provenance, ambiguous leads require staff.
- Semantic routing remains conservative keyword-assisted classification, not a fully verified semantic router.
- Secondary allowed channels now have separate internal state while preserving provider chat IDs. Legacy secondary-channel history is not copied; previously uncertain operations still require operator reconciliation.
- Provider acceptance is not a DELIVERED receipt; receipt mapping/reconciliation still needs implementation.
- Automated end-to-end acceptance matrix, sensitive-data retention policy/legal review and production webhook/CRM availability remain release blockers.

No real patient messages or bookings were created. No production deploy is authorized by this checkpoint.

## Resume verification evidence
- Prior interrupted work retained in local main checkpoint fd01f23.
- Fresh first full suite: 955 passed / 97 failed (intermediate tree, resume-full1.xml).
- Updated booking/integration subset: 400 passed / 2 failed; the two named-positive/question parsing regressions were then fixed.
- Medical follow-up: 47 passed. Webhook/channel follow-up: 35 passed. Previously failing FAQ/scenario follow-up: 27 passed.
- pip check: no broken requirements. pip-audit: no known vulnerabilities in declared requirements. Compile and git diff --check passed.
- Final full suite evidence is saved outside the repository at outputs/neurobalance_2_2026-11-17/resume-final.xml. Its result must be inspected before a main push.

- Final full execution: 1044 passed / 13 failed / 1057 total. Seven failures reflected fixture revisions made after collection; rerunning all 13 failed nodes on the corrected tree produced 7 passed / 6 failed. All six remaining failures are POSIX mode 0600 assertions on Windows (backup, runtime storage and four webhook-auth cases), not hidden or skipped. Linux CI is required before claiming a green full suite.
- Independent review found no further actionable regressions in the final production-code delta. Production deployment remains 1a4d7d51-ca7c-4f0d-b03f-aad66d6d349f; autodeploy independently rechecked disabled on 2026-11-18.
