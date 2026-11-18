# Optional CRM session provenance contract

Status: proposed integration contract; CRM support is UNCONFIRMED. No live CRM capability, endpoint or lead creation has been verified. This document is not evidence that CRM currently implements the feature.

## Existing behavior
The bot uses the existing authenticated patient lookup, without changing its endpoint or phone parameter. CRM must explicitly classify a client as NEW before initial AI admission. Missing or invalid status fails closed. A local session timestamp, phone, previously observed lead ID or lead status NEW does not prove that a later CRM card was created by the AI session.

## Optional trusted response
After an explicitly NEW admission the bot generates and persists an opaque random `ai_admission_session_id`. An independently authorized CRM integration would need to bind the card it creates for that exact session to this identifier and return it in authenticated lookup results:

```json
{"ok": true, "found": true, "isNew": false,
 "lead": {"id": "crm-card-id", "status": "IN_PROGRESS", "aiSessionId": "opaque-session-identifier"}}
```

The field is optional and backward compatible. This bot change does not transmit the token to an invented endpoint. The CRM owner must define the trusted transport and write path, prohibit client-controlled values from populating aiSessionId, and verify the resulting response before deployment. User messages, documents and model-extracted facts must never populate this field or the bot session token.

## Admission enforcement
Continuation as NEW_LEAD_IN_PROGRESS requires exact random-token, normalized-phone and lead-identity matching, an open session no older than 12 hours, and consistent evidence across all lookup variants. Patient history, active booking, assigned staff, takeover, closed session, malformed response and changed lead veto continuation. The primary legacy crm_patient_state remains NEW_PATIENT for existing gateway compatibility; lead_admission_state carries NEW_LEAD_IN_PROGRESS.

Without trusted returned provenance the transition to isNew=false is handed to staff; it is never silently admitted by a local lease. No full implementation or successful real exchange is claimed until the external owner implements and verifies the contract.

## Acceptance tests before activation
Use synthetic contacts and a non-production CRM: first lookup absent/NEW; integration creates card bound to token; subsequent lookup returns matching aiSessionId; bot continues after restart. Verify missing/mismatched token, different phone/card, history, active booking, human assignment, stale/closed session, and user-injected identifier all fail closed. Do not create test patient bookings in production.
