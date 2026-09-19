"""Look up whether a package name actually exists in its registry.

This is the core of slopcheck: LLM-generated dependency lists sometimes
include package names that were never real (the model pattern-matched
a plausible-looking name). Attackers register those exact names ahead
of time ("slopsquatting"), so a hallucinated import can become a real
supply-chain compromise the moment someone runs `pip install` on
AI-written code. Checking existence catches the hallucination before
install time.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Status = Literal["ok", "not_found", "recent", "error"]

_TIMEOUT = 10
_RECENT_DAYS = 30
_USER_AGENT = "slopcheck (+https://github.com/experimental-gains/slopcheck)"


@dataclass
class LookupResult:
    status: Status
    detail: str = ""


def _get_json(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _days_since(iso_timestamp: str) -> float:
    created = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400


def check_pypi(name: str) -> LookupResult:
    try:
        data = _get_json(f"https://pypi.org/pypi/{name}/json")
    except Exception as exc:  # noqa: BLE001 - surfaced as a non-fatal "error" row
        return LookupResult("error", str(exc))
    if data is None:
        return LookupResult("not_found", "no such project on PyPI")

    upload_times = [
        file_info["upload_time_iso_8601"]
        for release_files in data.get("releases", {}).values()
        for file_info in release_files
        if "upload_time_iso_8601" in file_info
    ]
    if not upload_times:
        return LookupResult("ok")
    age_days = _days_since(min(upload_times))
    if age_days < _RECENT_DAYS:
        return LookupResult("recent", f"first published {age_days:.0f} days ago")
    return LookupResult("ok")


def check_npm(name: str) -> LookupResult:
    try:
        data = _get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='')}")
    except Exception as exc:  # noqa: BLE001
        return LookupResult("error", str(exc))
    if data is None:
        return LookupResult("not_found", "no such package on npm")

    created = data.get("time", {}).get("created")
    if not created:
        return LookupResult("ok")
    age_days = _days_since(created)
    if age_days < _RECENT_DAYS:
        return LookupResult("recent", f"first published {age_days:.0f} days ago")
    return LookupResult("ok")


CHECKERS = {
    "pypi": check_pypi,
    "npm": check_npm,
}
