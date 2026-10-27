import re


def digits_only(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def sanitize_kz_phone(phone: str | None) -> str:
    d = digits_only(phone)
    if len(d) == 10 and d.startswith(("6", "7")):
        return "7" + d
    if len(d) == 11 and d.startswith(("86", "87")):
        return "7" + d[1:]
    if len(d) == 11 and d.startswith(("76", "77")):
        return d
    return ""
