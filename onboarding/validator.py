from __future__ import annotations

import re
from datetime import date

from onboarding.http_client import HubClient
from onboarding.models import (
    NormalizedRequest,
    Outcome,
    REJECTED_CODES,
    ReferenceData,
    ValidationResult,
    sort_unique_reasons,
)


_CNP_DIGITS_RE = re.compile(r"^[0-9]{13}\Z")
_CNP_WEIGHTS: tuple[int, ...] = (2, 7, 9, 1, 4, 6, 3, 5, 8, 2, 7, 9)


def parse_and_validate_cnp(cnp: str) -> date | None:
    """Rule B-R02: return birth date if `cnp` satisfies all Romanian CNP checks, else None."""
    if not _CNP_DIGITS_RE.match(cnp):
        return None

    sex_century = int(cnp[0])
    if sex_century in (1, 2):
        century_base = 1900
    elif sex_century in (3, 4):
        century_base = 1800
    elif sex_century in (5, 6):
        century_base = 2000
    else:
        return None

    yy = int(cnp[1:3])
    mm = int(cnp[3:5])
    dd = int(cnp[5:7])
    try:
        birth_date = date(century_base + yy, mm, dd)
    except ValueError:
        return None

    county = int(cnp[7:9])
    if not ((1 <= county <= 46) or county in (51, 52)):
        return None

    if cnp[9:12] == "000":
        return None

    weighted_sum = sum(int(cnp[i]) * _CNP_WEIGHTS[i] for i in range(12))
    rem = weighted_sum % 11
    expected_check = 1 if rem == 10 else rem
    if int(cnp[12]) != expected_check:
        return None

    return birth_date


def compute_completed_years(birth_date: date, start_date: date) -> int:
    """Rule B-R03: completed years of age at `start_date` (Feb 29 matures on Mar 1 in non-leap years)."""
    before_birthday = (start_date.month, start_date.day) < (birth_date.month, birth_date.day)
    return start_date.year - birth_date.year - (1 if before_birthday else 0)


def validate_request(
    req: NormalizedRequest,
    ref: ReferenceData,
    hub: HubClient,
) -> ValidationResult:
    """Evaluate rules B-R01..B-R09 respecting exact precedence and omission rules."""
    # B-R01: structural check halts all remaining validation checks
    if not req.raw_valid_form or req.start_date is None:
        return ValidationResult(
            passed=False,
            outcome=Outcome.REJECTED,
            reason_codes=["INVALID_REQUEST"],
            target_packages=(),
            target_trainings=(),
        )

    codes: list[str] = []

    # B-R02: CNP validation (omits B-R03 and B-R08 if violated)
    birth_date = parse_and_validate_cnp(req.cnp)
    b_r02_failed = birth_date is None
    if b_r02_failed:
        codes.append("INVALID_CNP")

    # B-R03: age thresholds at start_date (omitted if B-R02 failed)
    if not b_r02_failed and birth_date is not None:
        age = compute_completed_years(birth_date, req.start_date)
        if age < ref.config.minimum_age:
            codes.append("UNDERAGE")
        elif age < ref.config.adult_age:
            codes.append("MINOR_EMPLOYEE")

    # B-R04: role and department existence (either violation omits B-R06 and B-R09)
    role_def = ref.roles.get(req.role_code)
    dept_def = ref.departments.get(req.department_code)
    b_r04_failed = False
    if role_def is None:
        codes.append("UNKNOWN_ROLE")
        b_r04_failed = True
    if dept_def is None:
        codes.append("UNKNOWN_DEPARTMENT")
        b_r04_failed = True

    # B-R05: start_date must not precede business_date
    if req.start_date < ref.business_date:
        codes.append("START_DATE_IN_PAST")

    # B-R06: role allowed in department (omitted if B-R04 failed; omits B-R09 if violated)
    b_r06_failed = False
    if not b_r04_failed and role_def is not None:
        if req.department_code not in role_def.allowed_departments:
            codes.append("ROLE_NOT_IN_DEPARTMENT")
            b_r06_failed = True

    # B-R07: manager existence and ACTIVE status
    if req.manager_id is None or req.manager_id not in ref.employees:
        codes.append("MANAGER_MISSING")
    elif ref.employees[req.manager_id].status != "ACTIVE":
        codes.append("MANAGER_INACTIVE")

    # B-R08: live Directory check for same CNP under a different request_id (omitted if B-R02 failed)
    if not b_r02_failed:
        existing_accounts = hub.get("/directory/accounts", {"cnp": req.cnp})
        for acc in existing_accounts:
            acc_cnp = str(acc.get("cnp") or "").strip()
            acc_req_id = acc.get("request_id")
            acc_req_id_norm = acc_req_id.strip() if isinstance(acc_req_id, str) else None
            if acc_cnp == req.cnp and acc_req_id_norm != req.request_id:
                codes.append("ALREADY_EMPLOYED")
                break

    # Target packages & trainings + B-R09 SoD check (omitted if B-R04 or B-R06 failed)
    target_packages: tuple[str, ...] = ()
    target_trainings: tuple[str, ...] = ()
    if not b_r04_failed and not b_r06_failed and role_def is not None and dept_def is not None:
        pkg_set = set(role_def.base_packages) | set(dept_def.addon_packages)
        target_packages = tuple(sorted(pkg_set))

        has_sod_conflict = False
        for pkg_code in target_packages:
            pkg_def = ref.access_packages.get(pkg_code)
            if pkg_def and (pkg_def.conflicts_with & pkg_set):
                has_sod_conflict = True
                break
        if has_sod_conflict:
            codes.append("SOD_CONFLICT")

        mandatory_trainings = {
            t.training_code for t in ref.trainings.values() if t.mandatory
        }
        target_trainings = tuple(sorted(mandatory_trainings | set(role_def.required_trainings)))

    ordered_codes = sort_unique_reasons(codes)
    if ordered_codes:
        outcome = (
            Outcome.REJECTED
            if any(code in REJECTED_CODES for code in ordered_codes)
            else Outcome.MANUAL_REVIEW
        )
        return ValidationResult(
            passed=False,
            outcome=outcome,
            reason_codes=ordered_codes,
            target_packages=target_packages,
            target_trainings=target_trainings,
        )

    return ValidationResult(
        passed=True,
        outcome=None,
        reason_codes=[],
        target_packages=target_packages,
        target_trainings=target_trainings,
    )
