from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


class Outcome:
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"


class StepStatus:
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    NO_SLOT = "NO_SLOT"


REJECTED_CODES: frozenset[str] = frozenset(
    {
        "INVALID_REQUEST",
        "INVALID_CNP",
        "UNDERAGE",
        "UNKNOWN_ROLE",
        "UNKNOWN_DEPARTMENT",
        "START_DATE_IN_PAST",
    }
)

# Fixed canonical ordering of reason codes so restarts always emit identical payloads.
CANONICAL_REASON_ORDER: tuple[str, ...] = (
    # Validation rules (B-R01 .. B-R09) in specification table order
    "INVALID_REQUEST",
    "INVALID_CNP",
    "UNDERAGE",
    "MINOR_EMPLOYEE",
    "UNKNOWN_ROLE",
    "UNKNOWN_DEPARTMENT",
    "START_DATE_IN_PAST",
    "ROLE_NOT_IN_DEPARTMENT",
    "MANAGER_MISSING",
    "MANAGER_INACTIVE",
    "ALREADY_EMPLOYED",
    "NO_EXISTING_ACCOUNT",
    "SOD_CONFLICT",
    # Provisioning failures & warnings (B-R12, B-R13)
    "ACCOUNT_FAILED",
    "ACCESS_FAILED",
    "TRAINING_FAILED",
    "MEETING_FAILED",
    "MEETING_NOT_SCHEDULED",
)

_REASON_RANK: dict[str, int] = {code: idx for idx, code in enumerate(CANONICAL_REASON_ORDER)}


def sort_unique_reasons(codes: list[str] | set[str]) -> list[str]:
    """Deduplicate and sort reason codes in deterministic canonical order."""
    unique = set(codes)
    return sorted(unique, key=lambda c: (_REASON_RANK.get(c, 999), c))


@dataclass(frozen=True)
class RunConfig:
    minimum_age: int
    adult_age: int
    meeting_duration_minutes: int
    meeting_slot_minutes: int
    meeting_day_start: str
    meeting_day_end: str
    meeting_window_working_days: int
    meeting_timezone: str
    notification_recipients: dict[str, str]
    transfers_enabled: bool = False
    transfer_training_recency_months: int = 12
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> RunConfig:
        meeting = data.get("meeting") if isinstance(data.get("meeting"), dict) else {}
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        transfer = data.get("transfer") if isinstance(data.get("transfer"), dict) else {}
        recipients_raw = data.get("notification_recipients") or {}
        recipients = {
            str(k).strip().upper(): str(v).strip()
            for k, v in recipients_raw.items()
            if v is not None
        }
        transfers_enabled = bool(
            features.get("transfers", data.get("features.transfers", False))
        )
        recency_months = int(
            transfer.get(
                "training_recency_months",
                data.get("transfer.training_recency_months", 12),
            )
        )
        return cls(
            minimum_age=int(data["minimum_age"]),
            adult_age=int(data["adult_age"]),
            meeting_duration_minutes=int(
                meeting.get("duration_minutes", data.get("meeting.duration_minutes", 60))
            ),
            meeting_slot_minutes=int(
                meeting.get("slot_minutes", data.get("meeting.slot_minutes", 30))
            ),
            meeting_day_start=str(
                meeting.get("day_start", data.get("meeting.day_start", "09:00"))
            ).strip(),
            meeting_day_end=str(
                meeting.get("day_end", data.get("meeting.day_end", "17:00"))
            ).strip(),
            meeting_window_working_days=int(
                meeting.get("window_working_days", data.get("meeting.window_working_days", 3))
            ),
            meeting_timezone=str(
                meeting.get("timezone", data.get("meeting.timezone", "Europe/Bucharest"))
            ).strip(),
            notification_recipients=recipients,
            transfers_enabled=transfers_enabled,
            transfer_training_recency_months=recency_months,
            raw=data,
        )


@dataclass(frozen=True)
class RoleDef:
    role_code: str
    allowed_departments: frozenset[str]
    base_packages: tuple[str, ...]
    required_trainings: tuple[str, ...]


@dataclass(frozen=True)
class DepartmentDef:
    department_code: str
    addon_packages: tuple[str, ...]


@dataclass(frozen=True)
class AccessPackageDef:
    package_code: str
    conflicts_with: frozenset[str]


@dataclass(frozen=True)
class TrainingDef:
    training_code: str
    days_to_complete: int
    mandatory: bool


@dataclass(frozen=True)
class EmployeeDef:
    employee_id: str
    email: str
    department_code: str
    status: str


@dataclass(frozen=True)
class ReferenceData:
    business_date: date
    config: RunConfig
    roles: dict[str, RoleDef]
    departments: dict[str, DepartmentDef]
    access_packages: dict[str, AccessPackageDef]
    trainings: dict[str, TrainingDef]
    employees: dict[str, EmployeeDef]
    holidays: frozenset[date]


@dataclass(frozen=True)
class NormalizedRequest:
    request_id: str
    raw_valid_form: bool
    request_type: str
    first_name: str
    last_name: str
    cnp: str
    personal_email: str
    start_date: date | None
    start_date_iso: str
    role_code: str
    department_code: str
    manager_id: str | None


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    outcome: str | None  # REJECTED or MANUAL_REVIEW when passed is False
    reason_codes: list[str]
    target_packages: tuple[str, ...]
    target_trainings: tuple[str, ...]
    existing_username: str | None = None


@dataclass(frozen=True)
class ProvisioningResult:
    outcome: str  # COMPLETED or PARTIAL
    reason_codes: list[str]
    steps: dict[str, str]
    username: str | None = None
