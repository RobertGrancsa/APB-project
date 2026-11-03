from __future__ import annotations

from datetime import date
from typing import Any

from onboarding.calendar_utils import (
    add_working_days,
    build_busy_query_ranges,
    find_first_free_meeting_slot,
    get_meeting_window_days,
    parse_iso_datetime,
    subtract_months,
)
from onboarding.http_client import (
    FatalHubError,
    HubClient,
    IdempotencyKeyReusedError,
    ProvisioningStepError,
)
from onboarding.models import (
    NormalizedRequest,
    Outcome,
    ProvisioningResult,
    ReferenceData,
    StepStatus,
    ValidationResult,
    sort_unique_reasons,
)
from onboarding.normalizer import generate_base_username


def _find_existing_request_account(
    hub: HubClient, cnp: str, request_id: str
) -> str | None:
    accounts = hub.get("/directory/accounts", {"cnp": cnp})
    for acc in accounts:
        acc_cnp = str(acc.get("cnp") or "").strip()
        acc_req = acc.get("request_id")
        acc_req_norm = acc_req.strip() if isinstance(acc_req, str) else None
        if acc_cnp == cnp and acc_req_norm == request_id:
            return str(acc["username"]).strip()
    return None


def _find_first_account_by_cnp(hub: HubClient, cnp: str) -> str | None:
    accounts = hub.get("/directory/accounts", {"cnp": cnp})
    for acc in accounts:
        if str(acc.get("cnp") or "").strip() == cnp:
            return str(acc["username"]).strip()
    return None


def _pick_free_username(hub: HubClient, base_username: str) -> str:
    all_accounts = hub.get("/directory/accounts")
    taken = {
        str(acc.get("username") or "").strip().lower()
        for acc in all_accounts
        if acc.get("username")
    }
    if base_username not in taken:
        return base_username
    suffix = 2
    while f"{base_username}{suffix}" in taken:
        suffix += 1
    return f"{base_username}{suffix}"


def _active_grant_codes(grants_raw: list[dict[str, Any]]) -> set[str]:
    return {
        str(g.get("package_code") or "").strip().upper()
        for g in grants_raw
        if str(g.get("status") or "ACTIVE").strip().upper() == "ACTIVE"
    }


def _parse_optional_iso_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def provision_onboarding(
    req: NormalizedRequest,
    val: ValidationResult,
    ref: ReferenceData,
    hub: HubClient,
) -> ProvisioningResult:
    """Execute account, access, training, and meeting steps in order (rules B-R09..B-R13 + CR-1)."""
    assert req.start_date is not None
    assert req.manager_id is not None

    is_transfer = req.request_type == "TRANSFER"
    reason_codes: list[str] = []
    steps: dict[str, str] = {
        "account": StepStatus.SKIPPED,
        "access": StepStatus.SKIPPED,
        "training": StepStatus.SKIPPED,
        "meeting": StepStatus.SKIPPED,
    }

    # 1. account (B-R10)
    if is_transfer:
        username = val.existing_username or _find_first_account_by_cnp(hub, req.cnp)
        if username is None:
            raise FatalHubError(f"Missing existing account for TRANSFER {req.request_id}")
        steps["account"] = StepStatus.DONE
    else:
        username = _find_existing_request_account(hub, req.cnp, req.request_id)
        if username is not None:
            steps["account"] = StepStatus.DONE
        else:
            base_username = generate_base_username(req.first_name, req.last_name)
            candidate_username = _pick_free_username(hub, base_username)
            account_body = {
                "request_id": req.request_id,
                "username": candidate_username,
                "first_name": req.first_name,
                "last_name": req.last_name,
                "cnp": req.cnp,
                "role_code": req.role_code,
                "department_code": req.department_code,
                "start_date": req.start_date_iso,
            }
            try:
                hub.post(
                    "/directory/accounts",
                    account_body,
                    idempotency_key=f"{req.request_id}:account",
                    allow_provisioning_failure=True,
                )
                username = candidate_username
                steps["account"] = StepStatus.DONE
            except IdempotencyKeyReusedError as exc:
                recovered = _find_existing_request_account(hub, req.cnp, req.request_id)
                if recovered is None:
                    raise FatalHubError(str(exc)) from None
                username = recovered
                steps["account"] = StepStatus.DONE
            except ProvisioningStepError:
                steps["account"] = StepStatus.FAILED
                reason_codes.append("ACCOUNT_FAILED")
                return ProvisioningResult(
                    outcome=Outcome.PARTIAL,
                    reason_codes=sort_unique_reasons(reason_codes),
                    steps=steps,
                    username=None,
                )

    assert username is not None

    # 2. access (B-R09)
    access_failed = False
    if is_transfer:
        acct_grants = hub.get("/access/grants", {"username": username})
        active_codes = _active_grant_codes(acct_grants)
        target_set = set(val.target_packages)
        to_revoke = sorted(active_codes - target_set)
        to_grant = sorted(target_set - active_codes)

        for pkg_code in to_revoke:
            revoke_body = {
                "request_id": req.request_id,
                "username": username,
                "package_code": pkg_code,
            }
            try:
                hub.post(
                    "/access/revocations",
                    revoke_body,
                    idempotency_key=f"{req.request_id}:revoke:{pkg_code}",
                    allow_provisioning_failure=True,
                )
            except IdempotencyKeyReusedError as exc:
                refreshed = hub.get("/access/grants", {"username": username})
                if pkg_code in _active_grant_codes(refreshed):
                    raise FatalHubError(str(exc)) from None
            except ProvisioningStepError:
                access_failed = True
                reason_codes.append("ACCESS_FAILED")
                break

        if not access_failed:
            for pkg_code in to_grant:
                grant_body = {
                    "request_id": req.request_id,
                    "username": username,
                    "package_code": pkg_code,
                }
                try:
                    hub.post(
                        "/access/grants",
                        grant_body,
                        idempotency_key=f"{req.request_id}:grant:{pkg_code}",
                        allow_provisioning_failure=True,
                    )
                except IdempotencyKeyReusedError as exc:
                    refreshed = hub.get("/access/grants", {"username": username})
                    if pkg_code not in _active_grant_codes(refreshed):
                        raise FatalHubError(str(exc)) from None
                except ProvisioningStepError:
                    access_failed = True
                    reason_codes.append("ACCESS_FAILED")
                    break
    else:
        existing_grants = hub.get("/access/grants", {"request_id": req.request_id})
        granted_codes = _active_grant_codes(existing_grants)
        for pkg_code in val.target_packages:
            if pkg_code in granted_codes:
                continue
            grant_body = {
                "request_id": req.request_id,
                "username": username,
                "package_code": pkg_code,
            }
            try:
                hub.post(
                    "/access/grants",
                    grant_body,
                    idempotency_key=f"{req.request_id}:grant:{pkg_code}",
                    allow_provisioning_failure=True,
                )
            except IdempotencyKeyReusedError as exc:
                refreshed = hub.get("/access/grants", {"request_id": req.request_id})
                if pkg_code not in _active_grant_codes(refreshed):
                    raise FatalHubError(str(exc)) from None
            except ProvisioningStepError:
                access_failed = True
                reason_codes.append("ACCESS_FAILED")
                break

    steps["access"] = StepStatus.FAILED if access_failed else StepStatus.DONE

    # 3. training (B-R11)
    training_failed = False
    if is_transfer:
        cutoff_date = subtract_months(
            req.start_date, ref.config.transfer_training_recency_months
        )
        acct_enrollments = hub.get("/lms/enrollments", {"username": username})
        skip_codes: set[str] = set()
        for item in acct_enrollments:
            tr = str(item.get("training_code") or "").strip().upper()
            e_req = str(item.get("request_id") or "").strip()
            e_status = str(item.get("status") or "").strip().upper()
            completed_on = _parse_optional_iso_date(item.get("completed_on"))
            if e_req == req.request_id:
                skip_codes.add(tr)
            elif e_status == "COMPLETED" and completed_on is not None and completed_on >= cutoff_date:
                skip_codes.add(tr)
    else:
        existing_enrollments = hub.get("/lms/enrollments", {"request_id": req.request_id})
        skip_codes = {
            str(e.get("training_code") or "").strip().upper() for e in existing_enrollments
        }

    for tr_code in val.target_trainings:
        if tr_code in skip_codes:
            continue
        tr_def = ref.trainings[tr_code]
        due_date = add_working_days(
            req.start_date, tr_def.days_to_complete, ref.holidays
        ).isoformat()
        enrollment_body = {
            "request_id": req.request_id,
            "username": username,
            "training_code": tr_code,
            "due_date": due_date,
        }
        try:
            hub.post(
                "/lms/enrollments",
                enrollment_body,
                idempotency_key=f"{req.request_id}:training:{tr_code}",
                allow_provisioning_failure=True,
            )
        except IdempotencyKeyReusedError as exc:
            refreshed = hub.get("/lms/enrollments", {"request_id": req.request_id})
            refreshed_codes = {
                str(e.get("training_code") or "").strip().upper() for e in refreshed
            }
            if tr_code not in refreshed_codes:
                raise FatalHubError(str(exc)) from None
        except ProvisioningStepError:
            training_failed = True
            reason_codes.append("TRAINING_FAILED")
            break

    steps["training"] = StepStatus.FAILED if training_failed else StepStatus.DONE

    # 4. meeting (B-R12)
    existing_meetings = hub.get("/calendar/meetings", {"request_id": req.request_id})
    if existing_meetings:
        steps["meeting"] = StepStatus.DONE
    else:
        window_days = get_meeting_window_days(
            req.start_date,
            ref.config.meeting_window_working_days,
            ref.holidays,
        )
        if not window_days:
            steps["meeting"] = StepStatus.NO_SLOT
            reason_codes.append("MEETING_NOT_SCHEDULED")
        else:
            busy_intervals = []
            for chunk_from, chunk_to in build_busy_query_ranges(
                window_days[0], window_days[-1]
            ):
                busy_items = hub.get(
                    "/calendar/busy",
                    {
                        "employee_id": req.manager_id,
                        "from": chunk_from.isoformat(),
                        "to": chunk_to.isoformat(),
                    },
                )
                for item in busy_items:
                    busy_intervals.append(
                        (parse_iso_datetime(item["start"]), parse_iso_datetime(item["end"]))
                    )

            slot = find_first_free_meeting_slot(
                window_days,
                day_start=ref.config.meeting_day_start,
                day_end=ref.config.meeting_day_end,
                slot_minutes=ref.config.meeting_slot_minutes,
                duration_minutes=ref.config.meeting_duration_minutes,
                timezone_name=ref.config.meeting_timezone,
                busy_intervals=busy_intervals,
            )
            if slot is None:
                steps["meeting"] = StepStatus.NO_SLOT
                reason_codes.append("MEETING_NOT_SCHEDULED")
            else:
                start_iso, end_iso = slot
                meeting_body = {
                    "request_id": req.request_id,
                    "manager_id": req.manager_id,
                    "username": username,
                    "start": start_iso,
                    "end": end_iso,
                }
                try:
                    hub.post(
                        "/calendar/meetings",
                        meeting_body,
                        idempotency_key=f"{req.request_id}:meeting",
                        allow_provisioning_failure=True,
                    )
                    steps["meeting"] = StepStatus.DONE
                except IdempotencyKeyReusedError as exc:
                    refreshed_meetings = hub.get(
                        "/calendar/meetings", {"request_id": req.request_id}
                    )
                    if not refreshed_meetings:
                        raise FatalHubError(str(exc)) from None
                    steps["meeting"] = StepStatus.DONE
                except ProvisioningStepError:
                    steps["meeting"] = StepStatus.FAILED
                    reason_codes.append("MEETING_FAILED")

    has_failure = any(status == StepStatus.FAILED for status in steps.values())
    outcome = Outcome.PARTIAL if has_failure else Outcome.COMPLETED
    return ProvisioningResult(
        outcome=outcome,
        reason_codes=sort_unique_reasons(reason_codes),
        steps=steps,
        username=username,
    )
