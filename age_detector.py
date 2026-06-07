import re
from dataclasses import dataclass
from datetime import date

@dataclass
class DetectedAge:
    age: int
    source: str
    matched: str

def is_age_hard_stop(age: int) -> bool:
    return age < 16 or age > 74

def _calc_age(year: int, month: int, day: int, today: date) -> int:
    age = today.year - year
    if (today.month, today.day) < (month, day):
        age -= 1
    return age

def detect_age(text: str, today: date | None = None) -> DetectedAge | None:
    today = today or date.today()
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})[.\-/\s](\d{1,2})[.\-/\s](\d{4})(?:\s*г\.?)?\b", text)
    if m:
        d, mo, y = map(int, m.groups())
        if 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= today.year:
            age = _calc_age(y, mo, d, today)
            if 1 <= age <= 130:
                return DetectedAge(age=age, source="dob", matched=m.group(0))
    patterns = [
        r"(?:мне|маған|возраст|жасым)\s*(\d{1,3})\s*(?:лет|год|года|жас)?\b",
        r"\b(\d{1,3})\s*(?:лет|год|года|жас)\b",
    ]
    for p in patterns:
        m = re.search(p, text.lower())
        if m:
            age = int(m.group(1))
            if 1 <= age <= 130:
                return DetectedAge(age=age, source="explicit", matched=m.group(0))
    if re.fullmatch(r"\s*\d{1,3}\s*", text):
        age = int(text.strip())
        if 1 <= age <= 130:
            return DetectedAge(age=age, source="explicit", matched=text.strip())
    return None
