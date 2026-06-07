HARD_STOP_FIELDS = {
    "has_pacemaker_or_implant": "кардиостимулятор/имплант",
    "has_pregnancy": "беременность",
    "has_active_cancer": "онкология",
    "has_epilepsy": "эпилепсия",
}

SOFT_STOP_FIELDS = {
    "has_thrombosis_or_bleeding": "тромбоз/кровотечение",
    "has_decompensated_endocrine": "декомпенсированный СД/тиреотоксикоз",
    "has_acute_infection_or_fever": "острая инфекция/лихорадка",
    "has_severe_cardio_or_respiratory": "тяжёлая ССН/ДН",
    "has_severe_psychiatric": "психическое расстройство в обострении",
}

FIELD_LABELS = {
    "patient_age": "возраст",
    **HARD_STOP_FIELDS,
    **SOFT_STOP_FIELDS,
}


def evaluate_contraindications(args: dict) -> dict:
    age = int(args.get("patient_age") or 0)
    uncertain = list(args.get("uncertain_items") or [])

    hard_hits = [label for field, label in HARD_STOP_FIELDS.items() if args.get(field) is True]
    if age > 0 and not (16 <= age <= 74):
        hard_hits.append(f"возраст {age} лет (допустимо 16–74)")

    soft_hits = [label for field, label in SOFT_STOP_FIELDS.items() if args.get(field) is True]
    if age == 0 and "patient_age" not in uncertain:
        uncertain.append("patient_age")
    uncertain_labels = [FIELD_LABELS.get(x, x) for x in uncertain]

    if hard_hits:
        reason = "hard-stop: " + ", ".join(hard_hits)
        return {
            "verdict": "refuse",
            "reason": reason,
            "messageForBot": f"verdict=refuse; reason={reason}. Метод противопоказан. Вежливо откажи в записи и предложи очную консультацию врача для альтернативы. НЕ вызывай book_appointment.",
        }

    if soft_hits or uncertain_labels:
        parts = []
        if soft_hits:
            parts.append("soft-stop: " + ", ".join(soft_hits))
        if uncertain_labels:
            parts.append("не уточнено: " + ", ".join(uncertain_labels))
        reason = "; ".join(parts)
        return {
            "verdict": "escalate",
            "reason": reason,
            "messageForBot": f"verdict=escalate; reason={reason}. Запись делать НЕЛЬЗЯ. Передай координатору и вызови escalate_to_human.",
        }

    return {
        "verdict": "proceed",
        "reason": "no contraindications reported",
        "messageForBot": "verdict=proceed; reason=no contraindications reported. Противопоказаний нет — можно продолжать к book_appointment.",
    }
