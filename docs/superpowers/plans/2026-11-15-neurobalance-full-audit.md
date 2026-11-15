# Neuro Balance stabilization implementation plan

> For agentic workers: use superpowers:executing-plans; the explicit user instruction authorizes local execution immediately after the audit. Independent review follows implementation.

**Goal:** Correct the confirmed admission, patient-context, medical gate and booking reliability defects, with reproducible offline evidence and a reviewable PR.

**Architecture:** Preserve the GPT tool loop and external CRM contracts. Python retains conservative medical/admission/side-effect validation; uncertain outcomes fail closed. Keep sender identity separate from resettable patient facts.

**Tech Stack:** Python 3.12, FastAPI, OpenAI SDK, httpx, SQLite, pytest, GitHub Actions.

**Spec:** User task pasted 2026-11-15, `SYSTEM_PROMPT_rendered.md`, `AUDIT_REPORT.md`.

## Global constraints

- Do not change main, deploy/restart production, change variables/models, write real CRM appointments, send patient messages or edit the approved rendered prompt.
- Night window stays 20:00–08:00 Asia/Almaty; only verified new leads converse.
- Unknown relatives need staff verification; no fabricated separate-patient identity.
- Regression tests use synthetic SQLite and refused/mocked external transports.
- Retain facts already supplied; unknown/non-profile never advances the funnel.

## Review focus

- Stale facts returning from SQLite after patient switch; test round-trip persistence.
- Contradictory/negated mixed Russian/Kazakh symptoms and contraindications; literal table assertions.
- Multiple model tools in one round, emergency/refusal before POST; assert no CRM mutation.
- Timeout then different slot/restarted session; durable patient-wide claim must refuse.
- Conflicting phone formats or ambiguous relative status; never reinterpret an existing patient NEW.

## Tasks

### 1. Classifier and patient facts (parent owns services.py, agent.py, bot_tools.py)

- [x] Add failing table tests for approved complaints, relatives, negation, RU/KK and mixed scope; retain age/contra facts while UNCLEAR.
- [x] Implement conservative active-complaint classification and patient-context reset, exposing normalized relation and guardian confirmation in tool schema/context.
- [x] Run targeted tests, then complete suite. Explicitly update only expectations superseded by user requirements.

Interfaces: `services.classify_by_keywords(text)` preserves can_help True/False/None and adds profile_status/reason. `record_patient_facts` adds `guardian_accompaniment_confirmed`, preferred day/time and `patient_context_version` state.

### 2. Medical and transactional safety (parent owns agent.py/bot_tools.py)

- [x] Failing tests: no availability before medical clearance; ages16–18 require accompaniment; positive contra evidence cannot pass clear=true; unverified relatives cannot POST.
- [x] Failing tests: refusal/question not consent; all facts processed before automatic booking; no preflight stale-state POST.
- [x] Add durable patient-wide claim using existing state claim API; hold on timeout/unconfirmed/readback error, release only explicit rejection.
- [x] Preserve complete RU/KK contraindications and term explanations; align overrides with primary rules.
- [x] Verify targeted tests and full suite.

Interfaces: `bot_tools.booking_gate_status(session)` evaluates approved complaint, explicit contra clearance and guardian state; final booking additionally verifies patient identity and claims. No new external endpoint.

### 3. Admission, triage and persistence (integration worker owns crm.py, dialog.py, state.py and its new tests)

- [x] Reproduce and fix conflicting phone-format admission.
- [x] Add emergency check before agent invocation, retaining final check.
- [x] Add context-version-aware persistence so a new patient cannot inherit sticky contraindications/slots.
- [x] Implement only a provenance-bound NEW-lead continuation if CRM supplies enough identity evidence; all existing exclusion tests stay valid.
- [x] Targeted tests and full suite; return commands/evidence to parent.

Interfaces: parent increments `patient_context_version` on patient change; worker compares saved and incoming versions before sticky preservation. Any admission lease stores normalized phone + actual CRM lead ID and rejects unknown provenance.

### 4. Monitoring, >=60 dialogues and delivery (parent owns workflow, dialogue tests and reports)

- [x] Reproduce monitor mutation contract with a workflow behavior check or inspect workflow; remove unsolicited PATCH from ordinary monitoring.
- [x] Create at least60 named isolated multi-turn scenarios through real dialog/agent/tool pipeline with scripted model and CRM responses; assert responses, patient state and CRM side effects.
- [x] Compile, full pytest JUnit, pip-audit, diff/secret inspection, independent review and TDD fixes of findings. Windows POSIX permission failures remain documented; Linux CI is the target gate.
- [x] Produce all requested reports and safe deployment/rollback instructions, preserving raw test evidence under outputs.
- [ ] Commit and publish only dedicated branch; create PR (no merge/deploy). Recheck production SHA/main unchanged.
