from __future__ import annotations

import re

from onboarding.http_client import FatalHubError, HubClient, IdempotencyKeyReusedError
from onboarding.models import NormalizedRequest, Outcome, ReferenceData, StepStatus


_OUTCOME_NOTIFICATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    Outcome.COMPLETED: (
        ("MANAGER", "onboarding_completed"),
        ("IT", "it_setup"),
        ("EMPLOYEE", "welcome"),
    ),
    Outcome.PARTIAL: (
        ("HR", "onboarding_partial"),
        ("IT_OPS", "onboarding_partial"),
    ),
    Outcome.MANUAL_REVIEW: (
        ("HR", "manual_review"),
    ),
    Outcome.REJECTED: (
        ("HR", "request_rejected"),
    ),
}


def required_notifications(outcome: str, meeting_step: str) -> list[tuple[str, str]]:
    """Return ordered list of `(recipient_role, template)` pairs required for a recorded outcome."""
    pairs = list(_OUTCOME_NOTIFICATIONS[outcome])
    if outcome in (Outcome.COMPLETED, Outcome.PARTIAL) and meeting_step == StepStatus.NO_SLOT:
        pairs.append(("HR", "meeting_not_scheduled"))
    return pairs


def _resolve_recipient_email(
    role: str, req: NormalizedRequest, ref: ReferenceData
) -> str:
    if role == "MANAGER":
        if not req.manager_id or req.manager_id not in ref.employees:
            raise FatalHubError(
                f"Cannot resolve MANAGER email for request {req.request_id}: {req.manager_id!r}"
            )
        return ref.employees[req.manager_id].email
    if role == "EMPLOYEE":
        if not req.personal_email:
            raise FatalHubError(
                f"Cannot resolve EMPLOYEE email for request {req.request_id}"
            )
        return req.personal_email
    if role in ref.config.notification_recipients:
        return ref.config.notification_recipients[role]
    raise FatalHubError(f"Unknown notification recipient_role {role!r}")


def _build_safe_message(template: str, req: NormalizedRequest) -> str:
    """Build deterministic message solely from request data, strictly free of any digits."""
    raw_name = f"{req.first_name} {req.last_name}".strip()
    digit_free_name = re.sub(r"\d+", "", raw_name).strip()
    if digit_free_name:
        msg = f"Notificare {template} pentru {digit_free_name}."
    else:
        msg = f"Notificare {template}."
    return msg[:2000]


def send_missing_notifications(
    req: NormalizedRequest,
    outcome: str,
    meeting_step: str,
    ref: ReferenceData,
    hub: HubClient,
) -> None:
    """Send only notifications not yet present in `GET /notifications?request_id=...`."""
    expected = required_notifications(outcome, meeting_step)
    existing_raw = hub.get("/notifications", {"request_id": req.request_id})
    already_sent = {
        (
            str(item.get("recipient_role") or "").strip().upper(),
            str(item.get("template") or "").strip(),
        )
        for item in existing_raw
    }

    for role, template in expected:
        if (role, template) in already_sent:
            continue
        recipient = _resolve_recipient_email(role, req, ref)
        message = _build_safe_message(template, req)

        # Defensive assertion: never leak a 13-digit CNP in message
        if req.cnp and len(req.cnp) == 13 and req.cnp.isdigit():
            msg_digits = re.sub(r"\D+", "", message)
            if req.cnp in msg_digits:
                message = f"Notificare {template}."

        notif_body = {
            "request_id": req.request_id,
            "recipient_role": role,
            "template": template,
            "recipient": recipient,
            "message": message,
        }
        try:
            hub.post(
                "/notifications",
                notif_body,
                idempotency_key=f"{req.request_id}:notify:{role}:{template}",
                allow_provisioning_failure=False,
            )
        except IdempotencyKeyReusedError as exc:
            refreshed = hub.get("/notifications", {"request_id": req.request_id})
            refreshed_pairs = {
                (
                    str(item.get("recipient_role") or "").strip().upper(),
                    str(item.get("template") or "").strip(),
                )
                for item in refreshed
            }
            if (role, template) not in refreshed_pairs:
                raise FatalHubError(str(exc)) from None
