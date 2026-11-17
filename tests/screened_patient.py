"""Explicit prior checklist acceptance for pre-screened synthetic patients.

Only call from fixtures that model a completed screening; never autouse.
"""
import medical_rules


def accept_checklist(session):
    assert medical_rules.mark_checklist_presented(session, medical_rules.APPROVED_CONTRAINDICATIONS_RU)
    assert medical_rules.mark_clearance(session, "Ничего из перечисленного нет")
    return session
