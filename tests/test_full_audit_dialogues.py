"""Named offline acceptance conversations for the full-audit safety contract.

Every parameter is an isolated, multi-inbound conversation through the real
dialog -> model/tool loop -> SQLite path. OpenAI scripts specify decisions,
not medical/booking results: production tools must validate those decisions.
These tests establish backend safety and integration, not live-model quality.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent
import ai
import crm
import dialog
import state
from config import get_settings
from fake_openai import FakeOpenAIClient, FakeMessage, FakeResponse, assistant_text, assistant_tool_call

PHONE = "77011234567"
DAY = "2026-11-16"
LOGIN = "audit_doctor"
DOCTOR = "Аудит Тестович"
SAFE_REPLY = "Подскажите, какой следующий шаг Вам удобен?"
KK_REPLY = "Келесі қадамды нақтылайық. Қай күн ыңғайлы?"


class StrictClient(FakeOpenAIClient):
    """Never silently turn an exhausted script into a plausible answer."""
    def __init__(self, script):
        super().__init__(script)
        self.exhausted = False
        async def create(**kwargs):
            self.completions.calls.append(kwargs)
            if not self.completions._script:
                self.exhausted = True
                raise AssertionError("acceptance model script exhausted")
            step = self.completions._script.pop(0)
            if isinstance(step, Exception):
                raise step
            return FakeResponse(step)
        self.completions.create = create


class BoundaryCRM:
    def __init__(self):
        self.lookups = []
        self.availability = []
        self.books = []
        self.escalations = []
        self.doctor_calls = 0
        self.existing = False
        self.times = ["14:00", "15:40"]
        self.book_mode = "success"

    async def lookup(self, phone):
        self.lookups.append(phone)
        return {"ok": True, "found": self.existing, "isNew": not self.existing,
                "patient": {"id": "synthetic-existing"} if self.existing else None,
                "lead": None, "lastAppointment": None, "hasActiveAppointment": False,
                "appointment": None, "appointments": []}

    async def slots(self, date, doctor_login=None):
        self.availability.append({"date": date, "doctor_login": doctor_login})
        return {"ok": True, "date": date, "availability": [
            {"doctorLogin": LOGIN, "doctorName": DOCTOR, "date": date,
             "availableSlots": list(self.times)}]}

    async def doctors(self, force=False):
        self.doctor_calls += 1
        return {"ok": True, "doctors": [{"doctorLogin": LOGIN, "doctorName": DOCTOR,
                                         "specialization": "невролог"}]}

    async def book(self, **kwargs):
        self.books.append(kwargs)
        if self.book_mode == "timeout":
            raise TimeoutError("synthetic unknown POST outcome")
        if self.book_mode == "conflict":
            return {"ok": False, "error": "slot_conflict", "message": "slot already booked"}
        return {"ok": True, "id": "audit-9001", "status": "Записан",
                "date": kwargs["date"], "timeStart": kwargs["time_start"],
                "doctorName": kwargs.get("doctor_name") or DOCTOR}

    async def readback(self, appointment_id):
        if not self.books or self.book_mode == "readback_missing":
            return {}
        row = self.books[-1]
        return {"id": "audit-9001", "phone": row["phone"], "doctorLogin": row["doctor_login"],
                "date": row["date"], "timeStart": row["time_start"]}

    async def escalate(self, **kwargs):
        self.escalations.append(kwargs)
        return {"ok": True}


class Conversation:
    def __init__(self, monkeypatch, boundary, chat_id):
        self.monkeypatch = monkeypatch
        self.crm = boundary
        self.chat = chat_id
        self.clients = []
        self.answers = []
        self.decisions = []

    @property
    def session(self):
        # Always reload SQLite, never assert only an in-memory tool argument.
        return state.get_session(self.chat)

    def turn(self, text, *script):
        client = StrictClient(list(script))
        self.monkeypatch.setattr(ai, "_openai_client", lambda key: client)
        answer = asyncio.run(dialog.handle_message(self.chat, PHONE, text))
        assert not client.exhausted, "unplanned model call hidden by production exception handling"
        assert not client.completions._script, "script not consumed: real agent loop did not execute"
        # This dialog harness models a provider-accepted reply.
        import medical_rules
        saved = self.session
        if medical_rules.mark_checklist_presented(saved, answer):
            state.save_session(self.chat, saved)
        self.clients.append(client)
        self.answers.append(answer)
        self.decisions.append(list(self.session.get("agent_tool_calls") or []))
        assert isinstance(answer, str)
        return answer

    def facts(self, text, facts, reply=SAFE_REPLY):
        answer = self.turn(text, assistant_tool_call("record_patient_facts", facts), assistant_text(reply))
        assert answer.strip()
        assert "record_patient_facts" in self.decisions[-1]
        assert self.session.get("openai_used") is True
        return answer

    def available(self, text="Можно в понедельник?", reply=None, **extra):
        return self.turn(text, assistant_tool_call("get_available_slots", {
            "date_from": DAY, "days_ahead": 1, **extra}),
            assistant_text(reply or f"Свободно {DAY} в 14:00 и 15:40, врач {DOCTOR}. Какое время удобно?"))

    def result(self, key):
        rows = self.clients[-1].tool_results()
        matches = [row for row in rows if key in row]
        assert matches, (key, rows)
        return matches[-1]

    def ready(self, **extra):
        import medical_rules
        self.turn("Какие противопоказания?", assistant_text(medical_rules.APPROVED_CONTRAINDICATIONS_RU))
        text = f"Мне {extra.get('age', 34)}, болит поясница, противопоказаний нет"
        if extra.get("patient_name"):
            text += f", меня зовут {extra['patient_name']}"
        self.facts(text, {
            "complaint": "болит поясница", "age": 34, "contraindications_clear": True,
            "contraindications_note": "Противопоказаний нет", **extra})

    def book(self, text="Выбираю 14:00, имя пациента Асель", **extra):
        return self.turn(text, assistant_tool_call("book_appointment", {
            "patient_name": "Асель", "doctor_login": LOGIN, "date": DAY,
            "time_start": "14:00", **extra}), assistant_text("Проверяю результат записи."))


@pytest.fixture
def conv(monkeypatch, tmp_path, request):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "isolated-patients.sqlite3"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-synthetic-key")
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BRAIN_ENABLED", "true")
    monkeypatch.setenv("CRM_WRITE_ENABLED", "true")
    monkeypatch.setenv("NEW_LEADS_ONLY", "true")
    get_settings.cache_clear()
    state.init_db()
    monkeypatch.setattr(ai, "AsyncOpenAI", object)
    monkeypatch.setattr(agent.ai_budget, "check_allowed", lambda purpose: (True, ""))
    monkeypatch.setattr(agent.ai_budget, "record_usage", lambda *a, **k: {})
    boundary = BoundaryCRM()
    monkeypatch.setattr(crm, "lookup_active_appointments_by_phone", boundary.lookup)
    monkeypatch.setattr(crm, "check_slots", boundary.slots)
    monkeypatch.setattr(crm, "get_doctors", boundary.doctors)
    monkeypatch.setattr(crm, "book_appointment", boundary.book)
    monkeypatch.setattr(crm, "escalate_to_operator", boundary.escalate)
    monkeypatch.setattr(crm.CRMClient, "lookup_appointment_by_id", lambda self, appointment_id: boundary.readback(appointment_id))
    result = Conversation(monkeypatch, boundary, request.node.name)
    yield result
    assert not boundary.books or all(row["phone"] == PHONE for row in boundary.books)
    get_settings.cache_clear()


# Eleven distinct relative situations, including a sender already in the CRM.
@pytest.mark.parametrize("text,relation,complaint,age,status", [
    ("Маме 62, грыжа позвоночника", "mother", "грыжа позвоночника", 62, "profile"),
    ("Папе 58, болит колено", "father", "болит колено", 58, "profile"),
    ("Мужу 45, остеохондроз", "husband", "остеохондроз", 45, "profile"),
    ("Дочери 17, болит спина", "daughter", "болит спина", 17, "profile"),
    ("Сыну 12, последствия травмы колена", "son", "последствия травмы колена", 12, "profile"),
    ("Бабушке 79, артроз", "grandmother", "артроз", 79, "profile"),
    ("У жены болит только пятка, ей 40", "wife", "болит только пятка", 40, "non_profile"),
    ("Мама 52, болит поясница, противопоказаний нет", "mother", "болит поясница", 52, "profile"),
    ("Сестре 29, шея болит, можно в пятницу", "sister", "шея болит", 29, "profile"),
    ("Папа 60, артрит, я уже был у вас", "father", "артрит", 60, "profile"),
    ("Хочу записать ребёнка с болью в спине", "child", "боль в спине", None, "profile"),
], ids=["mother62-hernia", "father-knee", "husband45-osteochondrosis", "daughter17",
        "son12-trauma", "grandmother79", "wife-heel", "mother-explicit-clear",
        "sister-Friday", "existing-sender-father", "child-age-unknown"])
def test_relative_patient_identity_never_uses_sender_admission(conv, text, relation, complaint, age, status):
    facts = {"patient_relation": relation, "complaint": complaint}
    if "противопоказаний нет" in text:
        facts.update(contraindications_clear=True, contraindications_note="противопоказаний нет")
    if age is not None:
        facts["age"] = age
    if relation == "sister":
        facts["preferred_date_text"] = "пятница"
    conv.facts(text, facts)
    assert conv.session["patient_relation"] == relation
    assert conv.session["profile_status"] == status
    assert conv.session.get("age") == age if age is not None else not conv.session.get("age")
    assert not conv.session.get("contraindications_ok")  # No prior accepted checklist.
    if "я уже был" in text:
        conv.crm.existing = True
        answer = conv.turn("Точно для папы, не для меня")
        assert answer == ""
        assert conv.session.get("crm_patient_is_new") is False
    else:
        answer = conv.book("Запишите родственника, его имя Пациент Тест", patient_relation=relation)
        assert answer.strip()
        assert conv.result("error")["error"] == "relative_identity_unverified"
        assert "book_appointment" in conv.decisions[-1]
    assert not conv.crm.books
    assert not conv.session.get("booking_confirmed")
    assert len(conv.crm.lookups) >= 2


@pytest.mark.parametrize("first,complaint,status,clarify,clarified", [
    ("Спина не болит, болит только пятка", "спина не болит, болит только пятка", "non_profile", "Теперь болит колено", "болит колено"),
    ("Белім ауырмайды, тек өкшем ауырады", "белім ауырмайды, тек өкшем ауырады", "non_profile", "Тізем ауырады", "тізем ауырады"),
    ("Горло уже прошло, сейчас болит поясница", "горло уже прошло, сейчас болит поясница", "profile", "Мне 40", None),
    ("Болит колено и пятка", "болит колено и пятка", "unclear", "Нужна консультация по колену", "болит колено"),
    ("Мне плохо, 40 лет, противопоказаний нет", "мне плохо", "unclear", "Болит шея", "болит шея"),
    ("Болит живот", "болит живот", "unclear", "Болит поясница", "болит поясница"),
    ("Зуб болит", "зуб болит", "non_profile", "Теперь беспокоит сустав", "болит сустав"),
    ("Ауырсыну бар", "ауырсыну бар", "unclear", "Мойным ауырады", "мойным ауырады"),
    ("Болит спена", "болит спена", "profile", "Мне 40", None),
    ("Грыжи нет, болит только горло", "грыжи нет, болит только горло", "non_profile", "Болит спина", "болит спина"),
], ids=["negated-back-active-heel", "kk-negated-back-heel", "resolved-throat-current-back",
        "mixed-knee-heel", "unclear-retains-facts", "unknown-abdomen", "tooth-not-profile",
        "kk-unknown-clarifies-neck", "typo-back", "negated-hernia-throat"])
def test_complaint_context_restricts_medical_progress(conv, first, complaint, status, clarify, clarified):
    kk = "ауыр" in first.lower()
    reply = "Нақты қай жеріңіз ауырады?" if kk else "Уточните, пожалуйста, где именно болит?"
    supplied = first + (". Жасым 40, қарсы көрсетілімдер жоқ" if kk else ". Мне 40, противопоказаний нет")
    conv.facts(supplied, {"complaint": complaint, "age": 40, "contraindications_clear": True,
                       "contraindications_note": "нет"}, reply)
    assert conv.session["profile_status"] == status
    assert conv.session["age"] == 40
    assert conv.session.get("contraindications_ok") is not True
    if status != "profile":
        conv.available("Можно уже выбрать день?", reply="Сначала уточним жалобу.")
        assert conv.result("error")["error"]
        assert not conv.crm.availability
    conv.facts(clarify, {"complaint": clarified} if clarified else {"age": 40},
               KK_REPLY if kk else "Спасибо, данные сохранены. Какой день удобен?")
    assert conv.session["profile_status"] == "profile"
    assert conv.session["age"] == 40
    assert not conv.crm.books


@pytest.mark.parametrize("age,accompanies", [(16, True), (17, True), (18, True), (16, False), (17, False), (18, False)],
                         ids=["age16-guardian", "age17-guardian", "age18-guardian", "age16-alone", "age17-alone", "age18-alone"])
def test_adolescent_accompaniment_is_required_before_availability(conv, age, accompanies):
    conv.ready(age=age)
    conv.available(reply="Нужно уточнить сопровождение родителя.")
    assert not conv.crm.availability
    assert "guardian" in conv.result("error")["error"]
    conv.facts("Приду с мамой" if accompanies else "Приду один", {"guardian_accompaniment_confirmed": accompanies})
    conv.available(reply="Проверила свободное время.")
    assert bool(conv.crm.availability) is accompanies
    assert conv.session["guardian_accompaniment_confirmed"] is accompanies
    assert not conv.crm.books


@pytest.mark.parametrize("age", [12, 15, 76, 79], ids=["child12", "teen15", "age76", "grandparent79"])
def test_outside_age_limits_does_not_reach_crm_availability(conv, age):
    conv.ready(age=age)
    conv.available(reply="Автоматическая запись недоступна, нужна проверка администратора.")
    assert conv.session["age"] == age
    assert conv.result("error")["error"]
    assert not conv.crm.availability
    assert not conv.crm.books


@pytest.mark.parametrize("text,note,expected", [
    ("У меня кардиостимулятор", "противопоказаний нет", False),
    ("У меня эпилепсия", "эпилепсия", False),
    ("Я беременна", "беременность", False),
    ("Не знаю, возможно тромбоз", "возможно тромбоз", False),
    ("Менде кардиостимулятор бар, қазақша жауап беріңіз", "нет", False),
    ("Жүктілік бар", "жүктілік бар", False),
    ("Что такое кардиостимулятор?", "что такое кардиостимулятор?", None),
    ("Эпилепсии нет, противопоказаний нет", "эпилепсии нет, противопоказаний нет", True),
], ids=["contradictory-clear-pacemaker", "epilepsy-present", "pregnancy-present", "thrombosis-uncertain",
        "kk-pacemaker", "kk-pregnancy", "term-question-not-diagnosis", "explicit-negation-clear"])
def test_contraindication_evidence_cannot_be_overruled_by_model_boolean(conv, text, note, expected):
    conv.facts("Болит спина, мне 34", {"complaint": "болит спина", "age": 34})
    import medical_rules
    conv.turn("Какие противопоказания?", assistant_text(medical_rules.APPROVED_CONTRAINDICATIONS_RU))
    answer = conv.facts(text, {"contraindications_clear": True, "contraindications_note": note},
                        "Кардиостимулятор — устройство для поддержания ритма сердца. Есть ли что-либо из перечисленного?"
                        if expected is None else KK_REPLY if "бар" in text else "Спасибо, уточним медицинские ограничения.")
    assert answer.strip()
    if expected is None:
        assert "устройство для поддержания ритма сердца" in answer
    assert bool(conv.session.get("contraindications_ok")) is (expected is True)
    conv.available("Можно в понедельник?", reply="Продолжим после проверки противопоказаний.")
    assert bool(conv.crm.availability) is (expected is True)
    assert not conv.crm.books


@pytest.mark.parametrize("topic,question,marker,stage", [
    ("price_first_visit", "Сколько стоит приём?", "5 000", "age"),
    ("address", "Где вы находитесь?", "Туран", "age"),
    ("mri_needed", "Нужно ли заранее МРТ?", "Сним", "contra"),
    ("installment", "Есть рассрочка?", "Каспи", "contra"),
    ("methods", "Какие методы используете?", "терап", "choice"),
    ("schedule", "Какой график?", "Пятница", "choice"),
], ids=["price-resumes-age", "address-resumes-age", "MRI-resumes-contra", "installment-resumes-contra",
        "methods-resumes-slot-choice", "schedule-resumes-slot-choice"])
def test_faq_answers_preserve_the_unfinished_booking_step(conv, topic, question, marker, stage):
    facts = {"complaint": "болит поясница"}
    if stage != "age":
        facts["age"] = 34
    if stage == "choice":
        import medical_rules
        conv.turn("Какие противопоказания?", assistant_text(medical_rules.APPROVED_CONTRAINDICATIONS_RU))
        facts.update(contraindications_clear=True, contraindications_note="нет")
    source = {"age": "Болит поясница", "contra": "Мне 34, болит поясница",
              "choice": "Мне 34, болит поясница, противопоказаний нет"}[stage]
    conv.facts(source, facts)
    if stage == "choice":
        conv.available()
    before = conv.session
    import clinic_info
    followup = {"age": "Сколько Вам лет?", "contra": "Есть ли противопоказания?",
                "choice": "Какое из предложенных времён Вам удобно?"}[stage]
    approved = clinic_info.CLINIC_INFO_TEMPLATES[topic]
    answer = conv.turn(question, assistant_tool_call("get_clinic_info", {"topic": topic}), assistant_text(approved + "\n\n" + followup))
    assert marker.lower() in answer.lower()
    if stage == "contra":
        from medical_rules import contraindication_checklist_text
        assert contraindication_checklist_text("ru") in answer
    else:
        assert followup in answer
    assert "get_clinic_info" in conv.decisions[-1]
    assert conv.result("topic")["topic"] == topic
    for key in ("complaint", "age", "contraindications_ok", "crm_offered_slots"):
        assert conv.session.get(key) == before.get(key)
    assert not conv.crm.books


@pytest.mark.parametrize("text", ["Не в 14:00", "14:00 не подходит", "В 14:00?", "Пока не записывайте", "Я передумал"],
                         ids=["negative-time", "time-does-not-fit", "time-question", "do-not-book-yet", "changed-mind"])
def test_selection_requires_current_affirmative_consent(conv, text):
    conv.ready()
    conv.available()
    before = len(conv.crm.books)
    # Adversarial model attempts booking despite the user's refusal/question.
    answer = conv.book(text)
    assert answer.strip()
    assert len(conv.crm.books) == before == 0
    assert not conv.session.get("booking_confirmed")
    assert conv.result("error")["error"]
    assert "book_appointment" in conv.decisions[-1]


@pytest.mark.parametrize("mode", ["success", "conflict", "timeout", "readback_missing"],
                         ids=["verified-success-once", "conflict-reoffers", "timeout-different-slot-blocked", "readback-missing-no-confirmation"])
def test_booking_requires_verified_crm_result_and_no_uncertain_second_post(conv, mode):
    conv.crm.book_mode = mode
    conv.ready()
    conv.available()
    answer = conv.book()
    assert len(conv.crm.books) == 1
    assert conv.crm.books[0]["doctor_login"] == LOGIN
    assert conv.crm.books[0]["time_start"] == "14:00"
    if mode == "success":
        assert conv.session.get("booking_confirmed") is True
        assert DAY in answer and "14:00" in answer and DOCTOR in answer
        assert conv.turn("Спасибо") == ""
        assert len(conv.crm.books) == 1
    else:
        assert not conv.session.get("booking_confirmed")
        assert "запись подтверждена" not in answer.lower()
        if mode == "conflict":
            conv.crm.times = ["15:40"]
            conv.available("Покажите другое время", reply=f"Доступно {DAY} в 15:40, врач {DOCTOR}.")
            assert len(conv.crm.availability) >= 2
        else:
            # An uncertain write hands ownership to a human. A later inbound
            # must be muted before the model can request a different POST.
            assert conv.session.get("manual_takeover") is True
            assert conv.turn("Тогда выбираю 15:40") == ""
            assert not conv.clients[-1].calls
            assert len(conv.crm.books) == 1, "unknown outcome must block a different slot too"


@pytest.mark.parametrize("question", ["Какие врачи принимают?", "Можно к доктору Несуществующему?", "Хочу к Аудит Тестович"],
                         ids=["list-real-doctors", "unknown-named-doctor", "known-named-doctor"])
def test_doctor_requests_always_consult_crm(conv, question):
    conv.facts("Болит колено, мне 40", {"complaint": "болит колено", "age": 40})
    answer = conv.turn(question, assistant_tool_call("get_doctors", {}),
                       assistant_text(f"В доступном списке есть врач {DOCTOR}. Есть ли противопоказания?"))
    assert DOCTOR in answer
    assert conv.crm.doctor_calls == 1
    assert "get_doctors" in conv.decisions[-1]
    assert conv.result("doctors")["doctors"]
    assert conv.session["age"] == 40
    assert not conv.crm.availability and not conv.crm.books


@pytest.mark.parametrize("first,second,reply1,reply2,lang", [
    ("Белім ауырады", "Жасым 34", "Жасыңыз нешеде?", "Қарсы көрсетілімдер бар ма?", "kk"),
    ("Болит спина", "Мне 34", "Сколько Вам лет?", "Есть ли противопоказания?", "ru"),
    ("Болит спина", "Қазақша жауап беріңіз, жасым 34", "Сколько Вам лет?", "Қарсы көрсетілімдер бар ма?", "kk"),
    ("Белім ауырады", "На русском, мне 34", "Жасыңыз нешеде?", "Есть ли противопоказания?", "ru"),
], ids=["Kazakh-persists", "Russian-persists", "switch-to-Kazakh", "switch-to-Russian"])
def test_two_language_patient_state_persists(conv, first, second, reply1, reply2, lang):
    from medical_rules import contraindication_checklist_text
    conv.facts(first, {"complaint": first}, reply1)
    answer = conv.facts(second, {"age": 34}, reply2)
    assert contraindication_checklist_text(lang) in answer
    assert conv.session["language"] == lang
    assert conv.session["age"] == 34
    assert conv.session["complaint"] == first
    assert len(conv.crm.lookups) == 2
    assert not conv.crm.availability and not conv.crm.books


@pytest.mark.parametrize("relation,new_complaint", [("mother", "грыжа позвоночника"), ("father", "болит колено"), ("wife", "пятка болит")],
                         ids=["self-to-mother-resets", "self-to-father-resets", "self-to-wife-nonprofile-resets"])
def test_patient_switch_cannot_inherit_previous_medical_clearance(conv, relation, new_complaint):
    conv.ready(patient_name="Отправитель")
    conv.available()
    before_version = int(conv.session.get("patient_context_version") or 0)
    conv.facts("Теперь запись для другого родственника", {"patient_relation": relation, "complaint": new_complaint})
    assert conv.session["patient_relation"] == relation
    assert conv.session.get("patient_context_version", 0) > before_version
    assert not conv.session.get("age")
    assert not conv.session.get("contraindications_ok")
    assert not conv.session.get("patient_name")
    assert not conv.session.get("selected_slot")
    conv.book("Запишите родственника", patient_relation=relation)
    assert conv.result("error")["error"] == "relative_identity_unverified"
    assert not conv.crm.books


@pytest.mark.parametrize("failure", [RuntimeError("synthetic model transport failure"), ValueError("synthetic malformed response")],
                         ids=["model-network-error", "model-response-error"])
def test_model_failure_after_progress_is_safe_and_nonempty(conv, failure):
    conv.facts("Болит поясница, мне 34", {"complaint": "болит поясница", "age": 34})
    answer = conv.turn("Можно продолжить?", failure)
    assert answer.strip()
    assert conv.session["complaint"] == "болит поясница"
    assert conv.session["age"] == 34
    assert conv.session.get("openai_skip_reason") == "openai_error"
    assert conv.crm.escalations
    assert not conv.crm.books and not conv.crm.availability


@pytest.mark.parametrize("lang,question", [("ru", "Какие противопоказания?"), ("kk", "Қарсы көрсетілімдер қандай?")],
                         ids=["full-Russian-approved-list", "full-Kazakh-approved-list"])
def test_complete_contraindication_list_is_enforced_in_both_languages(conv, lang, question):
    from medical_rules import contraindication_checklist_text
    conv.facts("Белім ауырады, жасым 34" if lang == "kk" else "Болит поясница, мне 34",
               {"complaint": "болит поясница", "age": 34},
               "Жалғастырайық." if lang == "kk" else "Давайте уточним ограничения.")
    answer = conv.turn(question, assistant_text("Қарсы көрсетілім — жүктілік." if lang == "kk" else "Противопоказание — беременность."))
    assert contraindication_checklist_text(lang) in answer
    assert answer.count("—") == 8
    assert "чек-лист" not in answer.lower()
    assert not conv.session.get("contraindications_ok")
    assert not conv.crm.availability and not conv.crm.books


@pytest.mark.parametrize("kind", ["none", "unknown-time", "unknown-doctor"],
                         ids=["no-CRM-times-no-invention", "unoffered-time-rejected", "unoffered-doctor-rejected"])
def test_empty_or_unknown_slot_cannot_create_booking(conv, kind):
    conv.ready()
    if kind == "none":
        conv.crm.times = []
        answer = conv.available(reply="На выбранный день свободного времени нет. Проверим другой день?")
        assert "нет" in answer
        assert conv.result("slots")["slots"] == []
    else:
        conv.available()
    conv.book("Хочу другое время", **({"time_start": "18:30"} if kind == "unknown-time"
                                      else {"doctor_login": "invented_doctor"} if kind == "unknown-doctor" else {}))
    assert conv.result("error")["error"]
    assert not conv.crm.books
    assert not conv.session.get("booking_confirmed")


@pytest.mark.parametrize("topic,question,needle", [("address", "Мекенжайыңыз қандай?", "Тұран"),
                                                  ("price_first_visit", "Қабылдау қанша тұрады?", "5 000")],
                         ids=["Kazakh-address-resumes-age", "Kazakh-price-resumes-age"])
def test_kazakh_faq_preserves_language_and_complaint(conv, topic, question, needle):
    import clinic_info
    conv.facts("Белім ауырады", {"complaint": "белім ауырады"}, "Жасыңыз нешеде?")
    answer = conv.turn(question, assistant_tool_call("get_clinic_info", {"topic": topic}),
                       assistant_text(clinic_info.CLINIC_INFO_TEMPLATES_KK[topic] + "\n\nЖасыңыз нешеде?"))
    assert needle in answer
    assert "Жасыңыз нешеде?" in answer
    assert conv.session["language"] == "kk"
    assert conv.session["complaint"] == "белім ауырады"
    assert not conv.session.get("age")
    assert "get_clinic_info" in conv.decisions[-1]
    assert not conv.crm.availability and not conv.crm.books
