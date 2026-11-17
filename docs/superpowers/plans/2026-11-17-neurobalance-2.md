# Neuro Balance 2 implementation plan

**Goal:** Implement the supplied clinic administrator specification while preserving the existing Wazzup/CRM contracts and live production.
**Spec:** [Full user specification](../../NEURO_BALANCE_2_SPEC_RU.md)
**Architecture:** Keep the FastAPI modular monolith and single-replica SQLite on the existing /data volume. Extend the durable inbox, independent medical gates and single tool-using dialog agent; do not replace working integrations. PostgreSQL migration is unnecessary for the verified single-writer deployment, but horizontal scaling remains prohibited.
**Tech stack:** Python, FastAPI, SQLite, httpx, OpenAI; bounded PDF rasterization and image validation.

## Constraints and acceptance
- Work directly on local main; never force push, create a feature branch or PR.
- Source main SHA: 4b8c928e3d3ef369b07debec96cb81f8db49d5d9. Local backup tag: backup/neurobalance-before-2-2026-11-17.
- Verify autodeploy disabled before any main push. Never deploy production.
- Preserve Wazzup URLs/channel/config contract, CRM endpoints, clinic rules and 20:00–08:00 Asia/Almaty shift.
- No real patient messages/bookings. Automated tests use synthetic inputs and offline providers. Real ASR/vision/LLM quality needs separate evidence and must not be claimed from mocks.
- Patient-specific clearance only after all eight canonical contraindications; missing rules close booking.
- Every received event has a durable controlled result; uncertain side effects are never blindly retried.

## Tasks
1. [ ] Audit and reproduce: run current suite, retain evidence, import prior audit regression tests and observe failures before reusing the reviewed fixes from aa5d0c0. Preserve original backup.
2. [ ] Medical rules and dialog: validate canonical source integrity, checklist presentation/clearance, patient separation and existing booking guards. Record CRM provenance limitation if current contract cannot prove ownership.
3. [ ] Media: TDD voice language auto-detection; secure bounded image/PDF ingestion; preserve caption/all parts; gate external medical processing on operator configuration and keep failures visible.
4. [ ] Ingress/reliability: TDD attachment dedupe, voice/document routing, daytime retention, immediate staff takeover and final send gate; expose retained attention states.
5. [ ] Review: independent reviewer evaluates safety, specification and integration diff. Fix actionable findings with regression tests.
6. [ ] Verification/delivery: full suite, compile, dependency audit, synthetic integration contracts, sanitized diff. Document all unavailable real-provider checks and rollout/rollback steps. Commit tested work on main; push only after autodeploy isolation verified.

## Review focus
Voice-only and caption+document events; takeover while an AI call is awaiting; day/night boundary before send; uncertain CRM/provider result after restart; canonical rules absent or patient changes.

## Decisions and evidence
The user explicitly instructed autonomous implementation beyond audit/planning, so execution proceeds without an extra design approval. Previous PR work is source material only; no PR is being created or merged. Baseline test execution and each red/green cycle are captured under outputs/neurobalance_2_2026-11-17 outside the repository.
