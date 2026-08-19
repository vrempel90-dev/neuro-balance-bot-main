# Neuro Balance real-production repair contract

You are repairing the Neuro Balance WhatsApp AI administrator from REAL production evidence only.

Input on stdin is JSON from the live Wazzup production stream for the last few minutes. It has already been redacted for phone numbers and direct identifiers. Do not invent additional conversations, CRM responses, slots, states, or bugs.

## Absolute rules

1. A code change is allowed only when a concrete real exchange in the supplied events shows a plausible software/dialogue defect.
2. Before changing production code, create a NEW regression test named `tests/real_wazzup_regression_<short_fingerprint>.py` from that exact real case.
3. Run that test against the current checkout BEFORE the fix. It must fail for the concrete reason seen in production. If it passes, cannot reproduce, depends on missing live data, or only reflects subjective wording preference: delete the test and make NO code changes.
4. External CRM/Wazzup/network failure alone is not a code bug. Do not change code merely because an external service returned an error.
5. Never invent a CRM slot. `session.last_slots` / existing CRM availability logic remains authoritative.
6. Never claim booking/cancellation/reschedule succeeded unless existing Python/CRM state proves it.
7. Preserve the booking order and safety gates: complaint -> age -> contraindications -> date -> CRM slots -> time -> name -> booking.
8. `SYSTEM_PROMPT_rendered.md` remains the canonical behavior source. Python remains authoritative for state transitions, contraindications, slots and booking safety.
9. Preserve RU/KK behavior and language guards.
10. Do NOT modify any Wazzup, CRM, deployment, scheduling, environment-variable, webhook, Railway, or public API contract.
11. Do NOT modify `main.py`, `crm.py`, `wazzup.py`, `config.py`, `schedule.py`, Railway files, or workflow files.
12. Allowed production files are only: `dialog.py`, `ai.py`, `bot_tools.py`, `language_guard.py`, `strict_prompt_guard.py`, `clinic_info.py`.
13. Make the smallest possible fix. Do not refactor unrelated code.
14. After the fix, the new regression test must pass.
15. Run relevant nearby existing tests as well. If your change breaks an existing deterministic test, revert your code and leave the repository unchanged.

## High-confidence production defect signals

Treat these as investigation signals, not automatic proof:

- unexpected empty answer while the bot should have replied;
- loop/repeated question with no forward progress;
- `asked_name_too_early` or another guard repeatedly destroying a valid flow;
- slot hallucination or selected time not present in real CRM-derived slots;
- false booking confirmation;
- language mismatch RU/KK;
- state moves backward or skips a mandatory clinical gate;
- a side question causes the booking flow to be lost;
- a real customer message reproducibly produces an exception or invalid answer.

A guard firing can be correct. If the guard safely blocked an unsafe model decision and the final patient-facing response is correct, do not change anything.

## Required working method

- Inspect the supplied event sequence by `conversation_id` and timestamp.
- Find the smallest concrete case.
- Read only the code needed to explain it.
- Add one focused regression test from the actual wording/state shown in the event stream.
- Run it before the fix and verify it fails.
- Apply the minimal correction.
- Run the same test and relevant adjacent tests.
- Leave only the regression test and necessary production-code change in the working tree.

If no real reproducible defect is present, finish without touching files. A no-change run is a successful outcome.
