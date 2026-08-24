"""Runtime tightening for the GPT-first agent prompt.

SYSTEM_PROMPT_rendered.md is now the canonical production prompt. The historical
AGENT_OVERRIDES block duplicated many rules and contained one conflicting rule
that told the model to request CRM slots immediately on booking intent, before
age/contraindication gates. Keep only the small technical reinforcement that is
useful to the agent loop.
"""
from __future__ import annotations

import agent


STRICT_AGENT_OVERRIDES = """
TECHNICAL AGENT RULES — these reinforce, never replace, the canonical prompt:
- You conduct the conversation; Python executes tools and validates hard rules.
- Persist every booking-relevant fact with record_patient_facts as soon as the patient states it.
- Do NOT call get_available_slots until complaint, age, contraindications clearance and the requested day are known.
- Doctors, dates and times come only from current CRM tool results.
- book_appointment may use only the exact doctor_login/date/time from a CRM-offered option selected by the patient.
- Never claim a booking exists until book_appointment returns booking_success=true.
- Answer FAQ briefly, then continue the currently missing booking step.
- Return normal patient-facing text, not JSON. Never return an empty reply.
""".strip()


def install_agent_prompt_policy() -> None:
    agent.AGENT_OVERRIDES = STRICT_AGENT_OVERRIDES
