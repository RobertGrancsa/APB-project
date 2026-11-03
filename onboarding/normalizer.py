from __future__ import annotations

import re
from datetime import date
from typing import Any

from onboarding.models import NormalizedRequest


REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "request_type",
    "first_name",
    "last_name",
    "cnp",
    "personal_email",
    "start_date",
    "role_code",
    "department_code",
)

_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DOT_DATE_RE = re.compile(r"^[0-9]{2}\.[0-9]{2}\.[0-9]{4}\Z")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+\Z")

_CEDILLA_TO_COMMA = str.maketrans(
    {
        "\u015f": "\u0219",  # ş -> ș
        "\u0163": "\u021b",  # ţ -> ț
        "\u015e": "\u0218",  # Ş -> Ș
        "\u0162": "\u021a",  # Ţ -> Ț
    }
)

_DIACRITIC_TO_ASCII = str.maketrans(
    {
        "ă": "a",
        "â": "a",
        "î": "i",
        "ș": "s",
        "ş": "s",
        "ț": "t",
        "ţ": "t",
        "Ă": "a",
        "Â": "a",
        "Î": "i",
        "Ș": "s",
        "Ş": "s",
        "Ț": "t",
        "Ţ": "t",
    }
)


def normalize_person_name(raw: str) -> str:
    """Rule N4: normalize diacritics, spaces/hyphens, and Title Case per word/hyphen-part."""
    s = raw.strip().translate(_CEDILLA_TO_COMMA)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s)

    def _cap(segment: str) -> str:
        return (segment[0].upper() + segment[1:].lower()) if segment else ""

    return " ".join("-".join(_cap(hp) for hp in word.split("-")) for word in s.split(" "))


def parse_start_date(raw: str) -> date | None:
    """Rule N5: parse exact YYYY-MM-DD or DD.MM.YYYY into a real calendar date, else None."""
    s = raw.strip()
    if _ISO_DATE_RE.match(s):
        year, month, day = int(s[0:4]), int(s[5:7]), int(s[8:10])
    elif _DOT_DATE_RE.match(s):
        day, month, year = int(s[0:2]), int(s[3:5]), int(s[6:10])
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _ascii_slug_part(text: str) -> str:
    lowered = text.translate(_DIACRITIC_TO_ASCII).lower()
    cleaned = re.sub(r"[^a-z-]", "", lowered)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned


def generate_base_username(first_name: str, last_name: str) -> str:
    """Rule B-R10 steps 1-4: build base username `<first_word_of_first_name>.<last_name_hyphenated>`."""
    first_words = [w for w in re.split(r"[\s-]+", first_name.strip()) if w]
    first_token = first_words[0] if first_words else ""
    family_token = re.sub(r"\s+", "-", last_name.strip())

    given_slug = _ascii_slug_part(first_token)
    family_slug = _ascii_slug_part(family_token)
    return f"{given_slug}.{family_slug}"


def normalize_request(request_id: str, doc: dict[str, Any]) -> NormalizedRequest:
    """Apply rules N1-N6 and evaluate structural prerequisites for B-R01."""
    raw_valid = True
    for field_name in REQUIRED_STRING_FIELDS:
        val = doc.get(field_name)
        if not isinstance(val, str) or not val.strip():
            raw_valid = False
            break

    # Extract normalized strings safely even when malformed (needed e.g. for THIS_RUN notifications)
    req_type = doc["request_type"].strip() if isinstance(doc.get("request_type"), str) else ""
    first_name = (
        normalize_person_name(doc["first_name"])
        if isinstance(doc.get("first_name"), str) and doc["first_name"].strip()
        else ""
    )
    last_name = (
        normalize_person_name(doc["last_name"])
        if isinstance(doc.get("last_name"), str) and doc["last_name"].strip()
        else ""
    )
    cnp = doc["cnp"].strip() if isinstance(doc.get("cnp"), str) else ""
    personal_email = (
        doc["personal_email"].strip().lower() if isinstance(doc.get("personal_email"), str) else ""
    )
    role_code = doc["role_code"].strip().upper() if isinstance(doc.get("role_code"), str) else ""
    department_code = (
        doc["department_code"].strip().upper() if isinstance(doc.get("department_code"), str) else ""
    )

    raw_mgr = doc.get("manager_id")
    manager_id: str | None = None
    if isinstance(raw_mgr, str) and raw_mgr.strip():
        manager_id = raw_mgr.strip().upper()

    parsed_date: date | None = None
    if isinstance(doc.get("start_date"), str) and doc["start_date"].strip():
        parsed_date = parse_start_date(doc["start_date"])

    if raw_valid:
        if req_type != "NEW_HIRE":
            raw_valid = False
        elif parsed_date is None:
            raw_valid = False
        elif not _EMAIL_RE.match(personal_email):
            raw_valid = False

    return NormalizedRequest(
        request_id=request_id,
        raw_valid_form=raw_valid,
        request_type=req_type,
        first_name=first_name,
        last_name=last_name,
        cnp=cnp,
        personal_email=personal_email,
        start_date=parsed_date,
        start_date_iso=parsed_date.isoformat() if parsed_date else "",
        role_code=role_code,
        department_code=department_code,
        manager_id=manager_id,
    )
