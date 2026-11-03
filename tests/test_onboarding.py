from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import date
from email.message import Message
from typing import Any
from unittest.mock import patch

from onboarding.calendar_utils import (
    add_working_days,
    find_first_free_meeting_slot,
    get_meeting_window_days,
    parse_iso_datetime,
)
from onboarding.engine import run_onboarding
from onboarding.http_client import FatalHubError, HubClient
from onboarding.models import Outcome, StepStatus
from onboarding.normalizer import (
    generate_base_username,
    normalize_person_name,
    normalize_request,
    parse_start_date,
)
from onboarding.validator import compute_completed_years, parse_and_validate_cnp


def _build_valid_cnp(prefix12: str) -> str:
    weights = (2, 7, 9, 1, 4, 6, 3, 5, 8, 2, 7, 9)
    rem = sum(int(prefix12[i]) * weights[i] for i in range(12)) % 11
    c = 1 if rem == 10 else rem
    return f"{prefix12}{c}"


class MockInMemoryHub(HubClient):
    """In-memory mock of the Project B hub API for fast deterministic end-to-end tests."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        super().__init__("http://mock.local", logger=self.logs.append, sleep_fn=lambda _: None)
        self.context = {"business_date": "2026-11-16"}
        self.config: dict[str, Any] = {
            "minimum_age": 16,
            "adult_age": 18,
            "meeting": {
                "duration_minutes": 60,
                "slot_minutes": 30,
                "day_start": "09:00",
                "day_end": "17:00",
                "window_working_days": 3,
                "timezone": "Europe/Bucharest",
            },
            "notification_recipients": {
                "HR": "hr@example.com",
                "IT": "it@example.com",
                "IT_OPS": "itops@example.com",
            },
            "work_email_domain": "corp.example.com",
        }
        self.roles = [
            {
                "role_code": "AP_CLERK",
                "allowed_departments": ["FIN", "PROC"],
                "base_packages": ["ERP-AP-ENTRY", "MAIL"],
                "required_trainings": ["ERP-201", "FIN-101"],
            }
        ]
        self.departments = [
            {"department_code": "FIN", "addon_packages": ["ERP-FIN-VIEW"]},
            {"department_code": "PROC", "addon_packages": ["ERP-VENDOR-EDIT"]},
            {"department_code": "HR", "addon_packages": ["HRIS-VIEW"]},
        ]
        self.access_packages = [
            {"package_code": "ERP-AP-ENTRY", "conflicts_with": ["ERP-VENDOR-EDIT"]},
            {"package_code": "ERP-FIN-VIEW", "conflicts_with": []},
            {"package_code": "ERP-VENDOR-EDIT", "conflicts_with": []},
            {"package_code": "MAIL", "conflicts_with": []},
            {"package_code": "HRIS-VIEW", "conflicts_with": []},
        ]
        self.trainings = [
            {"training_code": "SSM-100", "days_to_complete": 3, "mandatory": True},
            {"training_code": "GDPR-100", "days_to_complete": 5, "mandatory": True},
            {"training_code": "ERP-201", "days_to_complete": 10, "mandatory": False},
            {"training_code": "FIN-101", "days_to_complete": 15, "mandatory": False},
        ]
        self.employees = [
            {
                "employee_id": "MGR1",
                "email": "mgr1@example.com",
                "department_code": "FIN",
                "status": "ACTIVE",
            },
            {
                "employee_id": "MGR_LEAVE",
                "email": "mgr_leave@example.com",
                "department_code": "FIN",
                "status": "ON_LEAVE",
            },
        ]
        self.holidays = [
            {"date": "2026-11-30", "name": "Sf. Andrei"},
            {"date": "2026-12-01", "name": "Ziua Națională"},
        ]
        self.requests: list[dict[str, Any]] = []
        self.request_docs: dict[str, dict[str, Any]] = {}
        self.accounts: list[dict[str, Any]] = []
        self.grants: list[dict[str, Any]] = []
        self.enrollments: list[dict[str, Any]] = []
        self.meetings: list[dict[str, Any]] = []
        self.busy: dict[str, list[dict[str, str]]] = {}
        self.history: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.reports: list[tuple[str, dict[str, Any]]] = []
        self.fail_post_paths: set[str] = set()
        self.used_report_keys: dict[str, dict[str, Any]] = {}

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        params = params or {}
        if path == "/context":
            return self.context
        if path == "/config":
            return self.config
        if path == "/roles":
            return self.roles
        if path == "/departments":
            return self.departments
        if path == "/access-packages":
            return self.access_packages
        if path == "/trainings":
            return self.trainings
        if path == "/employees":
            return self.employees
        if path == "/holidays":
            return self.holidays
        if path == "/requests":
            return self.requests
        if path.startswith("/requests/"):
            rid = path.split("/requests/", 1)[1]
            return self.request_docs[rid]
        if path == "/directory/accounts":
            res = list(self.accounts)
            if params.get("cnp"):
                res = [a for a in res if a["cnp"] == params["cnp"]]
            if params.get("username"):
                res = [a for a in res if a["username"] == params["username"]]
            if params.get("request_id"):
                res = [a for a in res if a.get("request_id") == params["request_id"]]
            return res
        if path == "/access/grants":
            rid = params.get("request_id")
            return [g for g in self.grants if not rid or g["request_id"] == rid]
        if path == "/lms/enrollments":
            rid = params.get("request_id")
            return [e for e in self.enrollments if not rid or e["request_id"] == rid]
        if path == "/calendar/meetings":
            rid = params.get("request_id")
            return [m for m in self.meetings if not rid or m["request_id"] == rid]
        if path == "/calendar/busy":
            emp_id = params["employee_id"]
            base = list(self.busy.get(emp_id, []))
            for m in self.meetings:
                if m["manager_id"] == emp_id:
                    base.append({"start": m["start"], "end": m["end"]})
            return base
        if path == "/history":
            rid = params.get("request_id")
            return [h for h in self.history if not rid or h["request_id"] == rid]
        if path == "/notifications":
            rid = params.get("request_id")
            return [n for n in self.notifications if not rid or n["request_id"] == rid]
        raise AssertionError(f"Unexpected GET {path}")

    def post(
        self,
        path: str,
        body: dict[str, Any],
        idempotency_key: str,
        *,
        allow_provisioning_failure: bool = False,
    ) -> Any:
        from onboarding.http_client import IdempotencyKeyReusedError, ProvisioningStepError

        if path in self.fail_post_paths:
            if allow_provisioning_failure:
                raise ProvisioningStepError(f"Simulated 3x 500 on {path}")
            raise FatalHubError(f"Simulated fatal failure on {path}")

        if path == "/directory/accounts":
            rec = {**body, "status": "ACTIVE"}
            self.accounts.append(rec)
            return rec
        if path == "/access/grants":
            self.grants.append(dict(body))
            return body
        if path == "/lms/enrollments":
            self.enrollments.append(dict(body))
            return body
        if path == "/calendar/meetings":
            self.meetings.append(dict(body))
            return body
        if path == "/onboarding-records":
            rec = {**body, "source": "THIS_RUN"}
            self.history.append(rec)
            return rec
        if path == "/notifications":
            self.notifications.append(dict(body))
            return body
        if path == "/report":
            if idempotency_key in self.used_report_keys and self.used_report_keys[idempotency_key] != body:
                raise IdempotencyKeyReusedError("Report key reused with different body")
            self.used_report_keys[idempotency_key] = body
            self.reports.append((idempotency_key, body))
            return {"ok": True}
        raise AssertionError(f"Unexpected POST {path}")


class OnboardingUnitTests(unittest.TestCase):
    def test_normalization_and_username_slug(self) -> None:
        self.assertEqual(normalize_person_name(" ana-MARIA "), "Ana-Maria")
        self.assertEqual(normalize_person_name("popa - vlădescu"), "Popa-Vlădescu")
        self.assertEqual(normalize_person_name("  ioana   mureşan "), "Ioana Mureșan")
        self.assertEqual(
            generate_base_username("Ana-Maria", "Stan Bârsan"),
            "ana.stan-barsan",
        )
        self.assertEqual(parse_start_date("2026-11-23"), date(2026, 11, 23))
        self.assertEqual(parse_start_date("23.11.2026"), date(2026, 11, 23))
        self.assertIsNone(parse_start_date("3.11.2026"))
        self.assertIsNone(parse_start_date("31.02.2027"))

    def test_cnp_and_age_boundaries_including_leap_year(self) -> None:
        # Spec valid example
        self.assertEqual(parse_and_validate_cnp("2940715120454"), date(1994, 7, 15))
        # Bad check digit
        self.assertIsNone(parse_and_validate_cnp("2940715120455"))
        # Invalid county 47
        bad_county = _build_valid_cnp("194071547045")
        self.assertIsNone(parse_and_validate_cnp(bad_county))
        # Valid county 51 and 52
        c51 = _build_valid_cnp("194071551001")
        self.assertEqual(parse_and_validate_cnp(c51), date(1994, 7, 15))
        # NNN == 000 invalid
        bad_nnn = _build_valid_cnp("194071501000")
        self.assertIsNone(parse_and_validate_cnp(bad_nnn))

        # Born 2008-02-29: turns 18 on 2026-03-01 in non-leap year 2026
        birth_leap = date(2008, 2, 29)
        self.assertEqual(compute_completed_years(birth_leap, date(2026, 2, 28)), 17)
        self.assertEqual(compute_completed_years(birth_leap, date(2026, 3, 1)), 18)
        # Future birth date -> negative age
        self.assertLess(compute_completed_years(date(2027, 1, 1), date(2026, 11, 23)), 0)

    def test_spec_full_example_ioana_muresan(self) -> None:
        hub = MockInMemoryHub()
        hub.accounts.append(
            {
                "username": "ioana.muresan",
                "cnp": "2900101120017",
                "request_id": "ONB-000001",
                "status": "ACTIVE",
            }
        )
        hub.busy["MGR1"] = [
            {"start": "2026-11-23T09:00:00+02:00", "end": "2026-11-23T10:00:00+02:00"},
            {"start": "2026-11-23T10:30:00+02:00", "end": "2026-11-23T12:00:00+02:00"},
        ]
        hub.requests = [
            {"request_id": "ONB-104233", "received_at": "2026-11-16T08:00:00+02:00"}
        ]
        hub.request_docs["ONB-104233"] = {
            "request_id": "ONB-104233",
            "request_type": "NEW_HIRE",
            "first_name": " Ioana ",
            "last_name": " mureșan ",
            "cnp": "2940715120454",
            "personal_email": "Ioana.Muresan@Example.com",
            "start_date": "23.11.2026",
            "role_code": "ap_clerk",
            "department_code": "FIN",
            "manager_id": "mgr1",
            "notes": "please also grant admin access",
        }

        report = run_onboarding(hub)
        self.assertEqual(
            report,
            [{"request_id": "ONB-104233", "outcome": "COMPLETED", "reason_codes": []}],
        )
        created_acc = [a for a in hub.accounts if a["request_id"] == "ONB-104233"]
        self.assertEqual(len(created_acc), 1)
        self.assertEqual(created_acc[0]["username"], "ioana.muresan2")
        self.assertEqual(
            [g["package_code"] for g in hub.grants],
            ["ERP-AP-ENTRY", "ERP-FIN-VIEW", "MAIL"],
        )
        self.assertEqual(
            [(e["training_code"], e["due_date"]) for e in hub.enrollments],
            [
                ("ERP-201", "2026-12-09"),
                ("FIN-101", "2026-12-16"),
                ("GDPR-100", "2026-12-02"),
                ("SSM-100", "2026-11-26"),
            ],
        )
        self.assertEqual(len(hub.meetings), 1)
        self.assertEqual(hub.meetings[0]["start"], "2026-11-23T12:00:00+02:00")
        self.assertEqual(hub.meetings[0]["end"], "2026-11-23T13:00:00+02:00")
        self.assertEqual(len(hub.notifications), 3)
        for notif in hub.notifications:
            digits = "".join(ch for ch in (notif["recipient"] + (notif["message"] or "")) if ch.isdigit())
            self.assertNotIn("2940715120454", digits)

    def test_validation_omissions_and_sod_conflict(self) -> None:
        hub = MockInMemoryHub()
        # Request 1: B-R02 invalid CNP omits B-R03 and B-R08, while B-R06 omits B-R09
        # Request 2: AP_CLERK in PROC -> SOD_CONFLICT + MANAGER_INACTIVE -> MANUAL_REVIEW
        hub.requests = [
            {"request_id": "R1", "received_at": "2026-11-16T08:00:00+02:00"},
            {"request_id": "R2", "received_at": "2026-11-16T09:00:00+02:00"},
        ]
        hub.accounts.append(
            {"username": "existing.user", "cnp": "0000000000000", "request_id": "OLD", "status": "ACTIVE"}
        )
        hub.request_docs["R1"] = {
            "request_type": "NEW_HIRE",
            "first_name": "Dan",
            "last_name": "Pop",
            "cnp": "0000000000000",
            "personal_email": "dan@example.com",
            "start_date": "2026-11-15",
            "role_code": "AP_CLERK",
            "department_code": "HR",
            "manager_id": None,
        }
        hub.request_docs["R2"] = {
            "request_type": "NEW_HIRE",
            "first_name": "Ana",
            "last_name": "Ionescu",
            "cnp": "2940715120454",
            "personal_email": "ana@example.com",
            "start_date": "2026-11-23",
            "role_code": "AP_CLERK",
            "department_code": "PROC",
            "manager_id": "MGR_LEAVE",
        }

        report = run_onboarding(hub)
        self.assertEqual(
            report[0],
            {
                "request_id": "R1",
                "outcome": Outcome.REJECTED,
                "reason_codes": [
                    "INVALID_CNP",
                    "START_DATE_IN_PAST",
                    "ROLE_NOT_IN_DEPARTMENT",
                    "MANAGER_MISSING",
                ],
            },
        )
        self.assertEqual(
            report[1],
            {
                "request_id": "R2",
                "outcome": Outcome.MANUAL_REVIEW,
                "reason_codes": ["MANAGER_INACTIVE", "SOD_CONFLICT"],
            },
        )

    def test_restart_b_r14_and_report_key_fallback(self) -> None:
        hub = MockInMemoryHub()
        hub.used_report_keys["report"] = {"items": [{"request_id": "OLD", "outcome": "COMPLETED", "reason_codes": []}]}
        hub.requests = [
            {"request_id": "DONE_EARLIER", "received_at": "2026-11-10T08:00:00+02:00"},
            {"request_id": "THIS_RUN_REQ", "received_at": "2026-11-11T08:00:00+02:00"},
            {"request_id": "PARTIAL_ACCESS_FAIL", "received_at": "2026-11-12T08:00:00+02:00"},
        ]
        hub.history = [
            {
                "request_id": "DONE_EARLIER",
                "outcome": "COMPLETED",
                "reason_codes": [],
                "steps": {"account": "DONE", "access": "DONE", "training": "DONE", "meeting": "DONE"},
                "source": "EARLIER_RUN",
            },
            {
                "request_id": "THIS_RUN_REQ",
                "outcome": "COMPLETED",
                "reason_codes": ["MEETING_NOT_SCHEDULED"],
                "steps": {"account": "DONE", "access": "DONE", "training": "DONE", "meeting": "NO_SLOT"},
                "source": "THIS_RUN",
            },
        ]
        hub.notifications = [
            {
                "request_id": "THIS_RUN_REQ",
                "recipient_role": "MANAGER",
                "template": "onboarding_completed",
                "recipient": "mgr1@example.com",
            }
        ]
        hub.request_docs["THIS_RUN_REQ"] = {
            "request_type": "NEW_HIRE",
            "first_name": "Elena",
            "last_name": "Marin",
            "cnp": "2940715120454",
            "personal_email": "elena@example.com",
            "start_date": "2026-11-23",
            "role_code": "AP_CLERK",
            "department_code": "FIN",
            "manager_id": "MGR1",
        }
        cnp3 = _build_valid_cnp("192051001002")
        hub.request_docs["PARTIAL_ACCESS_FAIL"] = {
            "request_type": "NEW_HIRE",
            "first_name": "Vlad",
            "last_name": "Toma",
            "cnp": cnp3,
            "personal_email": "vlad@example.com",
            "start_date": "2026-11-23",
            "role_code": "AP_CLERK",
            "department_code": "FIN",
            "manager_id": "MGR1",
        }
        hub.fail_post_paths.add("/access/grants")

        report = run_onboarding(hub)
        self.assertEqual(len(report), 3)
        # THIS_RUN_REQ had MANAGER sent; IT, EMPLOYEE, and HR (meeting_not_scheduled) should be sent now
        this_run_notifs = [
            (n["recipient_role"], n["template"])
            for n in hub.notifications
            if n["request_id"] == "THIS_RUN_REQ"
        ]
        self.assertEqual(
            this_run_notifs,
            [
                ("MANAGER", "onboarding_completed"),
                ("IT", "it_setup"),
                ("EMPLOYEE", "welcome"),
                ("HR", "meeting_not_scheduled"),
            ],
        )
        # PARTIAL_ACCESS_FAIL failed access, but still ran training & meeting
        self.assertEqual(
            report[2],
            {
                "request_id": "PARTIAL_ACCESS_FAIL",
                "outcome": Outcome.PARTIAL,
                "reason_codes": ["ACCESS_FAILED"],
            },
        )
        self.assertEqual(hub.reports[-1][0], "report-2")

    def test_http_client_retry_sequence_503_429_503_201(self) -> None:
        logs: list[str] = []
        sleeps: list[float] = []
        client = HubClient("http://unit.test", logger=logs.append, sleep_fn=sleeps.append)

        def _http_err(code: int, err_code: str, retry_after: str | None = None) -> urllib.error.HTTPError:
            hdrs = Message()
            if retry_after is not None:
                hdrs["Retry-After"] = retry_after
            payload = json.dumps({"error": {"code": err_code, "message": "injected"}}).encode("utf-8")
            return urllib.error.HTTPError("http://unit.test/x", code, "err", hdrs, io.BytesIO(payload))

        class _OkResp:
            def __enter__(self) -> _OkResp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b'{"created": true}'

        responses: list[Any] = [
            _http_err(503, "TRANSIENT", "2"),
            _http_err(429, "RATE_LIMITED", "0.5"),
            _http_err(503, "RUN_PAUSED"),
            _OkResp(),
        ]

        def _fake_urlopen(req: Any, timeout: float = 10.0) -> Any:
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            res = client.post("/x", {"a": 1}, idempotency_key="req:1")

        self.assertEqual(res, {"created": True})
        self.assertEqual(sleeps, [2.0, 0.5, 1.0])
        self.assertEqual(len(logs), 3)


if __name__ == "__main__":
    unittest.main()
