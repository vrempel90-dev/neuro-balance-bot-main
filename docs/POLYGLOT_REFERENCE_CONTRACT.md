# Neuro Balance — Polyglot reference contract

Reference repository: `abiotov/polyglot-booking-agent`
Pinned reference commit: `30de6266ecf2be3bf2fee734e21d1896ee3d94f5`
Neuro baseline commit: `98587c37278373b0f269bad9f7aef9e9c4d750ab`

## Scope

Polyglot is an architectural reference for the internal conversational brain only.
It is **not** a replacement for Neuro Balance infrastructure.

The following production contracts are frozen and must not be changed by this refactor:

- Wazzup / WhatsApp webhook transport and payload contracts;
- Railway runtime/deployment contract;
- CRM endpoints, authentication, request/response formats and booking integration;
- existing booking/reschedule/cancel behavior that is already covered by regression tests;
- current persistence/state compatibility with production sessions;
- clinic operator templates and `SYSTEM_PROMPT_rendered.md` content unless a separate reviewed business change explicitly requests it.

## Source-of-truth hierarchy

1. Python safety/business invariants are absolute: state transitions, contraindications, CRM availability, selected slot, booking payload, working-hours gates and human takeover.
2. `SYSTEM_PROMPT_rendered.md` is the canonical source for user-facing clinic facts, tone, wording, RU/KK behavior and dialog policy.
3. Secondary AI prompts are task/protocol prompts only. They must not contradict or silently rewrite the canonical clinic prompt.
4. Mutable clinic facts such as profile, prices, address, MRI guidance, schedule, methods, promotions and operator style must not be duplicated in `OPENAI_DIALOG_BRAIN_SYSTEM_PROMPT`; the protocol may reference the canonical prompt but must not maintain a second copy of those facts.
5. If the canonical clinic prompt cannot be loaded, the model must fail closed for clinic-fact questions rather than inventing an answer.

## Polyglot invariants adopted

- The LLM converses; deterministic code decides whether an external action is valid.
- The model must never invent availability. Only CRM-derived slots may be offered or selected.
- A booking action must be impossible unless required gates are satisfied.
- Tool/action arguments are validated before touching the outside world.
- Language/date context is deterministic where Python can know it.
- A model failure must fail safe, not create an unbounded loop or unsafe external action.
- Multi-turn regression/evaluation scenarios are treated as a first-class production gate.

## Verified Neuro safeguards

- Canonical prompt precedence is regression-tested; secondary humanizer/protocol prompts cannot silently replace required clinic wording such as `окошко`.
- Slot provenance is enforced: a normal booking uses a CRM-derived slot from `session.last_slots`; a legacy in-flight session that lost `last_slots` must revalidate the exact doctor/date/time through the existing CRM availability call before booking.
- Cross-turn anti-loop behavior is regression-tested in RU/KK, including safe escalation after repeated mandatory questions, forward progress when data is already known and protection against false positives on FAQ answers.
- Mutable clinic facts have been removed from the dialog protocol prompt; `SYSTEM_PROMPT_rendered.md` remains the only clinic fact source while Python keeps safety/business authority.
- `.github/workflows/brain-contract.yml` provides a deterministic CI gate for prompt, slot, anti-loop, RU/KK, hallucination, booking, CRM source-of-truth, webhook idempotency and working-hours contracts without live OpenAI or live CRM calls.

## Deliberately not copied

- Polyglot channel adapters: Neuro keeps Wazzup/WhatsApp.
- Polyglot calendar adapter: Neuro keeps the existing CRM.
- Polyglot business qualification schema: Neuro keeps complaint → age → contraindications → date → CRM slots → time → name → booking.
- Polyglot user-facing prompt text: Neuro keeps `SYSTEM_PROMPT_rendered.md`.

## Migration rule

Every change must be incremental and reversible. A PR may change internal brain behavior only when existing regression tests remain green and new characterization tests cover the changed invariant. External integration contracts are out of scope unless explicitly approved in a separate change.
