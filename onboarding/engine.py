from __future__ import annotations

import urllib.parse
from datetime import date
from typing import Any, Callable

from onboarding.calendar_utils import parse_iso_datetime
from onboarding.http_client import FatalHubError, HubClient, IdempotencyKeyReusedError
from onboarding.models import (
    AccessPackageDef,
    DepartmentDef,
    EmployeeDef,
    Outcome,
    ReferenceData,
    RoleDef,
    RunConfig,
    StepStatus,
    TrainingDef,
)
from onboarding.normalizer import normalize_request
from onboarding.notifier import send_missing_notifications
from onboarding.provisioner import provision_onboarding
from onboarding.validator import validate_request


FINAL_EARLIER_OUTCOMES: frozenset[str] = frozenset(
    {
        Outcome.COMPLETED,
        Outcome.REJECTED,
        Outcome.MANUAL_REVIEW,
    }
)


def load_reference_data(hub: HubClient) -> ReferenceData:
    """Workflow step 1: read /context, /config, and all reference catalogs once."""
    context_raw = hub.get("/context")
    business_date = date.fromisoformat(str(context_raw["business_date"]).strip())

    config_raw = hub.get("/config")
    config = RunConfig.from_api(config_raw)

    roles_raw = hub.get("/roles")
    roles: dict[str, RoleDef] = {}
    for item in roles_raw:
        code = str(item["role_code"]).strip().upper()
        roles[code] = RoleDef(
            role_code=code,
            allowed_departments=frozenset(
                str(d).strip().upper() for d in (item.get("allowed_departments") or [])
            ),
            base_packages=tuple(
                str(p).strip().upper() for p in (item.get("base_packages") or [])
            ),
            required_trainings=tuple(
                str(t).strip().upper() for t in (item.get("required_trainings") or [])
            ),
        )

    departments_raw = hub.get("/departments")
    departments: dict[str, DepartmentDef] = {}
    for item in departments_raw:
        code = str(item["department_code"]).strip().upper()
        departments[code] = DepartmentDef(
            department_code=code,
            addon_packages=tuple(
                str(p).strip().upper() for p in (item.get("addon_packages") or [])
            ),
        )

    packages_raw = hub.get("/access-packages")
    access_packages: dict[str, AccessPackageDef] = {}
    for item in packages_raw:
        code = str(item["package_code"]).strip().upper()
        access_packages[code] = AccessPackageDef(
            package_code=code,
            conflicts_with=frozenset(
                str(c).strip().upper() for c in (item.get("conflicts_with") or [])
            ),
        )

    trainings_raw = hub.get("/trainings")
    trainings: dict[str, TrainingDef] = {}
    for item in trainings_raw:
        code = str(item["training_code"]).strip().upper()
        trainings[code] = TrainingDef(
            training_code=code,
            days_to_complete=int(item["days_to_complete"]),
            mandatory=bool(item.get("mandatory", False)),
        )

    employees_raw = hub.get("/employees")
    employees: dict[str, EmployeeDef] = {}
    for item in employees_raw:
        emp_id = str(item["employee_id"]).strip().upper()
        employees[emp_id] = EmployeeDef(
            employee_id=emp_id,
            email=str(item["email"]).strip(),
            department_code=str(item.get("department_code") or "").strip().upper(),
            status=str(item["status"]).strip().upper(),
        )

    holidays_raw = hub.get("/holidays")
    holidays_set: set[date] = set()
    for item in holidays_raw:
        raw_date = item["date"] if isinstance(item, dict) else item
        holidays_set.add(date.fromisoformat(str(raw_date).strip()))

    return ReferenceData(
        business_date=business_date,
        config=config,
        roles=roles,
        departments=departments,
        access_packages=access_packages,
        trainings=trainings,
        employees=employees,
        holidays=frozenset(holidays_set),
    )


def _canonical_sort_requests(requests_raw: list[dict[str, Any]]) -> list[str]:
    """Ensure canonical ordering by `(received_at timestamp, request_id)`."""
    indexed = []
    for idx, item in enumerate(requests_raw):
        req_id = str(item["request_id"]).strip()
        received_at = item.get("received_at")
        if isinstance(received_at, str) and received_at.strip():
            ts = parse_iso_datetime(received_at).timestamp()
            indexed.append((0, ts, req_id, idx))
        else:
            indexed.append((1, float(idx), req_id, idx))
    indexed.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[2] for t in indexed]


def _format_log_line(request_id: str, outcome: str, reason_codes: list[str]) -> str:
    codes_str = ",".join(reason_codes) if reason_codes else "NONE"
    return f"REQUEST {request_id} outcome={outcome} reasons=[{codes_str}]"


def _submit_final_report(hub: HubClient, report_items: list[dict[str, Any]]) -> None:
    """Workflow step 10: POST /report with `report`, falling back to `report-2`, `report-3`, ..."""
    body = {"items": report_items}
    suffix = 1
    while True:
        key = "report" if suffix == 1 else f"report-{suffix}"
        try:
            hub.post("/report", body, idempotency_key=key, allow_provisioning_failure=False)
            return
        except IdempotencyKeyReusedError:
            suffix += 1


def run_onboarding(
    hub: HubClient,
    logger: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Execute the full Project B workflow (steps 1-10) against `hub`."""
    log = logger or hub.logger

    # Step 1: bootstrap context, config, and catalogs once
    ref = load_reference_data(hub)

    # Step 2: fetch requests in canonical order
    requests_raw = hub.get("/requests")
    request_ids = _canonical_sort_requests(requests_raw)

    report_items: list[dict[str, Any]] = []

    for request_id in request_ids:
        # Step 3: inspect history and apply rule B-R14 on the LAST record in the list
        history = hub.get("/history", {"request_id": request_id})
        last_record = history[-1] if history else None

        if last_record is not None:
            source = str(last_record.get("source") or "").strip().upper()
            recorded_outcome = str(last_record["outcome"]).strip().upper()
            recorded_codes = [str(c) for c in (last_record.get("reason_codes") or [])]
            steps_obj = last_record.get("steps") or {}
            recorded_meeting_step = str(
                steps_obj.get("meeting") or StepStatus.SKIPPED
            ).strip().upper()

            if source == "THIS_RUN":
                # Already recorded in an earlier start of this run: send only missing notifications
                encoded_id = urllib.parse.quote(request_id, safe="")
                doc = hub.get(f"/requests/{encoded_id}")
                req = normalize_request(request_id, doc)
                send_missing_notifications(
                    req, recorded_outcome, recorded_meeting_step, ref, hub
                )
                log(_format_log_line(request_id, recorded_outcome, recorded_codes))
                report_items.append(
                    {
                        "request_id": request_id,
                        "outcome": recorded_outcome,
                        "reason_codes": recorded_codes,
                    }
                )
                continue

            if source == "EARLIER_RUN" and recorded_outcome in FINAL_EARLIER_OUTCOMES:
                # Already finalized in an earlier run: zero writes, zero notifications
                log(_format_log_line(request_id, recorded_outcome, recorded_codes))
                report_items.append(
                    {
                        "request_id": request_id,
                        "outcome": recorded_outcome,
                        "reason_codes": recorded_codes,
                    }
                )
                continue

        # Step 4: read and normalize request document
        encoded_id = urllib.parse.quote(request_id, safe="")
        doc = hub.get(f"/requests/{encoded_id}")
        req = normalize_request(request_id, doc)

        # Step 5: validate business rules B-R01..B-R09
        val = validate_request(req, ref, hub)

        # Step 6: execute provisioning steps if validation passed
        if not val.passed:
            assert val.outcome is not None
            outcome = val.outcome
            reason_codes = val.reason_codes
            steps = {
                "account": StepStatus.SKIPPED,
                "access": StepStatus.SKIPPED,
                "training": StepStatus.SKIPPED,
                "meeting": StepStatus.SKIPPED,
            }
        else:
            prov = provision_onboarding(req, val, ref, hub)
            outcome = prov.outcome
            reason_codes = prov.reason_codes
            steps = prov.steps

        # Step 7: save onboarding record before sending any notifications
        record_body = {
            "request_id": request_id,
            "outcome": outcome,
            "reason_codes": reason_codes,
            "steps": steps,
        }
        try:
            hub.post(
                "/onboarding-records",
                record_body,
                idempotency_key=f"{request_id}:record",
                allow_provisioning_failure=False,
            )
        except IdempotencyKeyReusedError as exc:
            refreshed_hist = hub.get("/history", {"request_id": request_id})
            if not refreshed_hist or str(refreshed_hist[-1].get("source") or "") != "THIS_RUN":
                raise FatalHubError(str(exc)) from None
            latest = refreshed_hist[-1]
            outcome = str(latest["outcome"]).strip().upper()
            reason_codes = [str(c) for c in (latest.get("reason_codes") or [])]
            steps = dict(latest.get("steps") or steps)

        # Step 8: send required notifications (never containing CNP)
        send_missing_notifications(req, outcome, steps["meeting"], ref, hub)

        # Step 9: log one line per request
        log(_format_log_line(request_id, outcome, reason_codes))
        report_items.append(
            {
                "request_id": request_id,
                "outcome": outcome,
                "reason_codes": reason_codes,
            }
        )

    # Step 10: submit final report
    _submit_final_report(hub, report_items)
    return report_items
