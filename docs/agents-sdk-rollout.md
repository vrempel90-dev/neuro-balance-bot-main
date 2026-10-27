# NeuroBalance — staged Agents SDK rollout

This branch is **not production-ready**. It adds a feature-gated OpenAI Agents
SDK runner that calls existing `agent.execute_tool` handlers for all CRM and
medical safety decisions.

## Runtime selection

- `AGENTS_SDK_ENABLED=false` (default): current GPT-first `agent.py` loop.
- `AGENTS_SDK_ENABLED=true`: `agents_sdk_runtime.run_agent_turn` after the
  existing Wazzup/CRM lead-admission checks in `dialog.py`.
- `AGENTS_SDK_MAX_TURNS=8`, `AGENTS_SDK_MAX_TOOL_CALLS=8` bound the run.
- `CRM_WRITE_ENABLED=false` in staging to prevent real bookings.
- Official clinic prompt: `SYSTEM_PROMPT_rendered.md`; all existing CRM
  tools and deterministic gates remain in `agent.py`.
- SDK tracing is deliberately disabled for patient privacy.

The SDK reuses `state.py` rather than adding a second SDK Session database:
the existing message log and structured session are passed to each run. The
current inbound message is deduplicated before delivery to the model.

## Critical infrastructure prerequisites (before enabling in production)

1. **Correct start command.** Railway currently reports
   `sh -c 'exit 0'` for the main, staging and acceptance services. That is
   not a working application server. After confirming the intended live
   deployment topology, configure the appropriate service to start with
   `uvicorn live_main:app --host 0.0.0.0 --port $PORT` and verify
   `/health` + inbound webhook, not just Railway SUCCESS.
2. **Durable state.** Railway's inspected NeuroBalance services had no
   attached volume. Unless `SQLITE_PATH` points to independently durable
   storage, SQLite sessions/booking claims can disappear on container
   replacement. Provision a persistent volume for every active environment,
   set `SQLITE_PATH` to the mounted path, migrate the data safely and
   verify conversation continuity after restart. The staging DB must be
   isolated from production.
3. **Safe staging.** Use non-production Wazzup credentials/channel or a
   local webhook replay; isolate CRM and keep writes disabled. Never replay
   personal medical messages in logs or external traces.
4. **Model verification.** Check that `AI_BRAIN_MODEL` is available to the
   project's API key and supports SDK Responses/function tools.
5. **Pre-deployment tests.** Run
   `python -m compileall -q agents_sdk_runtime.py response_policy.py`
   and `pytest -q tests/test_sdk_migration.py`, then the project's full
   regression suite and end-to-end synthetic dialogs. These focused tests
   are not a substitute for multi-turn clinical validation.
6. **Production rollout.** Only after staging sign-off, snapshot state,
   deploy with `AGENTS_SDK_ENABLED=false`, test health and webhook, then
   turn the flag on for a controlled window. Monitor error categories,
   CRM verification, latency and duplicate questions. Roll back the flag
   if a gate fails.

## Security constraints

- No tool may create a booking without the existing deterministic CRM
  availability, patient/age/complaint and contraindication checks.
- Never claim a booking succeeded without `booking_success=true` from CRM.
- Do not repeat a CRM booking POST after uncertain success.
- All URLs in outbound responses must come from approved clinic templates.
- Outbound `response_policy` checks are a **defense in depth**, not a
  complete medical fact checker. Clinical correctness still requires
  specialist-reviewed scenarios and authorized source data.
- No tracing of patient text, names, phone numbers or medical history.

## Current known limitations

- In-memory/ephemeral SQLite durability is **not fixed by this branch**.
- Existing production start commands are **not changed by this branch**.
- A model may still misunderstand nuanced language; SDK is not a substitute
  for a clinical regression suite and human escalation.
- Only the user-provided Instagram URL has been updated. The actual street
  address, approved therapies and medical rules have not been independently
  verified with the clinic.
- Dependency upgrade from `openai==1.59.7` to modern `openai` and
  `openai-agents==0.17.8` must pass the legacy regression suite.

**Do not merge or turn on the SDK until CI and staging validation pass.**
