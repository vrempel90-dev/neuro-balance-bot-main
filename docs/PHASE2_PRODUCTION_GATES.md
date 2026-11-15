# Phase 2 — release blockers and verified-identity contract

Status: **NOT READY FOR PRODUCTION**. This document is an implementation/runbook contract, **not** evidence that the external provider was changed.
Baseline: PR #116. Follow-up: PR #117.

## 1. Booking a relative — missing CRM identity capability

The current CRM `GET /api/bot/patient-lookup?phone=...` checks the **sender's phone**, not the separately named patient. A name, age, complaint, relationship, or sender's CRM status cannot prove that the relative is a new patient.

Required backend capability (endpoint and field names **must be designed with the CRM owner**; do not assume one exists):

- Verify the **actual patient identity** independently of the sender, using an approved, privacy-safe identifier and appropriate authorization. An unverified name and age are not sufficient to merge/select an existing patient.
- Return an explicit, auditable eligibility result: `new_verified`, `existing_verified`, or `uncertain`; record a reference to the checked patient, never a fabricated contact.
- Return a stable verification reference scoped to the conversation, patient and agreed validity period. Prevent reuse for a different patient or after identity changes.
- Ensure the subsequent booking request binds **that verified patient** and the sender's contact/relationship separately. The CRM must enforce the same verification atomically on write; client-side checks alone are insufficient.
- Fail closed on `existing_verified`, `uncertain`, identity mismatch, expired verification, HTTP errors or timeout. Escalate to operator without asserting that the relative was booked.
- Authorize and audit identity access; minimize medical/personal data in logs.

Acceptance examples: new mother verified -> eligible after other medical gates; existing mother -> no automatic booking; sender existing but mother separately new -> governed by the approved **sender-new-lead** policy (do not silently override); mother then father in same chat -> new verification; name-only/ambiguous lookup -> operator; CRM POST timeout -> durable patient-wide claim, no duplicate POST.

**Current safe behavior remains:** store patient facts, preserve sender separately, and escalate relative booking without a CRM POST. Do not disable this guard to improve conversion.

## 2. CRM HTTP 429

Production logs from 2026-11-15 recorded HTTP 429 during patient lookup. PR #117 reduces same-chat burst by serializing phone aliases, retries read-only lookup **once** with a bounded delay and fails closed on repeated rate limiting.

External confirmation required from CRM owner: actual quota scope, whether aliases share a rate bucket, supported canonical-phone query, `Retry-After` behavior, permissible request concurrency, and possibility of a **single canonical identity-resolution endpoint**. The code change is mitigation, **not** a verified rate-limit resolution. Do not cache previous patient status as blanket admission.

## 3. Wazzup webhook authorization — O01

Monitoring artifact showed `webhook_auth_enabled=false`. Do **not** blindly set `WEBHOOK_SECRET` or block requests before the provider's webhook URI supports the same token. `production_webhook_auth.secure_existing_webhook` already contains a bounded migration/readback flow but its execution mutates provider configuration.

Authorized cutover sequence:
1. Owner checks existing destination, subscriptions and provider capabilities **read-only**.
2. Take configuration snapshot; prepare a rollback procedure and a service window.
3. After explicit approval, configure provider URI/token through the existing single-patch-and-readback mechanism.
4. Verify authenticated delivery and unauthorized rejection with synthetic messages that cannot reach patient processing.
5. Confirm valid production deliveries, monitor errors, and keep old destination details for rollback.

Never include tokens, query strings with token or real patient payloads in logs or PR evidence.

## 4. Failed inbox + uncertain booking claims — O03

Historical snapshot: 236 `failed` inbox jobs; four `uncertain` booking claim records (not necessarily four distinct bookings). This is **not** an instruction to replay or clear them.

Owner-approved operational reconciliation:
1. Create verified SQLite snapshot/backup and restore-check it in isolation.
2. Read-only export **redacted identifiers/status/created_at/error_type**, grouped by failure cause and age; establish whether errors remain active.
3. For every uncertain booking claim, verify the outcome in CRM before any subsequent attempt. `confirmed` / `not created` / `still unknown` are separate conclusions, each with evidence.
4. Resolve quarantined jobs only after confirming remote side effects and using existing authorized reconciliation controls. Never automatically retry an entire conversation turn or bulk-delete failed jobs.
5. Monitor rate of **new** failed jobs separately from accumulated historical counts.

Current Railway connector does not grant direct read-only access to individual SQLite rows. Therefore reconciliation **has not been performed**.

## 5. Real-model dialogue quality

PR #116's 73 multi-turn scenarios use scripted model responses. Real OpenAI dialogue quality in Russian/Kazakh is **not established**.

To validate, run an isolated, synthetic evaluation using the **exact configured production model** with approved development-only OpenAI credentials, a capped budget, and mocked CRM/Wazzup. Cover relative identity, profile vs non-profile complaints, multiple facts, refusal, emergency, guardian requirements, FAQ and booking. Record outputs and review all medical/eligibility false positives. Do not invoke paid API calls until credentials/budget are explicitly authorized.

## Go/no-go

PR #117 only addresses a **local CRM request burst**. O01–O03 and live-model quality remain separate release gates. Keep PR #116 and PR #117 draft; do not merge/deploy until owner approves the remaining operational steps and evidence.
