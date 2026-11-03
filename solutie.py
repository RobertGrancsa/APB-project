#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

from onboarding.engine import run_onboarding
from onboarding.http_client import FatalHubError, HubClient, HubError


def _configure_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _resolve_base_url(argv: list[str]) -> str | None:
    if len(argv) >= 2 and argv[1].strip():
        return argv[1].strip()
    env_url = os.environ.get("BPA_BASE_URL", "").strip()
    if env_url:
        return env_url
    return None


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_streams()
    args = sys.argv if argv is None else argv
    base_url = _resolve_base_url(args)
    if not base_url:
        print(
            "ERROR: Missing BASE_URL (pass as first CLI argument or set BPA_BASE_URL).",
            file=sys.stderr,
            flush=True,
        )
        return 1

    def log(message: str) -> None:
        print(message, flush=True)

    client = HubClient(base_url=base_url, logger=log)
    try:
        run_onboarding(client, logger=log)
        return 0
    except FatalHubError as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        return 1
    except HubError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # pragma: no cover - unexpected runtime safeguard
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
