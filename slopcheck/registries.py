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

Status = Literal["ok", "not_found", "recent", "error", "private"]

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
        # bandit flags urlopen for arbitrary schemes (B310), but both call
        # sites below build url from a fixed https:// literal plus a name
        # already constrained to a safe character set (parsers._REQ_LINE_RE
        # for PyPI, urllib.parse.quote for npm) — the scheme/host can't be
        # attacker-influenced.
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # nosec B310
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

    releases = data.get("releases", {})
    release_files = [file_info for files in releases.values() for file_info in files]

    # A project can carry one or more registered release *versions* with
    # zero files ever uploaded for any of them -- confirmed live against a
    # real, currently-registered PyPI project: `pypi.org/pypi/
    # requests_extension/json` returns HTTP 200 with `"releases":
    # {"0.0.0": []}` (a release record exists, its file list is empty).
    # Real pip 25.1.1 fails an unqualified `pip install requests_extension`
    # outright: "ERROR: Could not find a version that satisfies the
    # requirement requests_extension (from versions: none)" / "ERROR: No
    # matching distribution found for requests_extension" -- the identical
    # failure shape as a name that was never registered at all. This is
    # deliberately narrower than "no releases at all" (`releases == {}`,
    # left as the pre-existing conservative "ok" default below): Warehouse
    # never creates a Project record without at least one completed
    # release, so a genuine 200 response with zero release *versions*
    # doesn't appear to be reachable in practice, while a version existing
    # with an empty file list clearly is.
    if releases and not release_files:
        return LookupResult("not_found", "project has no installable release files on PyPI")

    upload_times = [
        file_info["upload_time_iso_8601"]
        for file_info in release_files
        if "upload_time_iso_8601" in file_info
    ]
    if not upload_times:
        return LookupResult("ok")

    # PEP 592: a real installer ignores every yanked release unless a caller
    # pins to its exact version (`==`/`===`) -- and slopcheck's Dependency
    # carries no version constraint at all (see this module's own docstring
    # and parsers.py's: "Only the package name is needed ... since the whole
    # point is checking whether the name exists in a registry at all"), so
    # it always models the unqualified `pip install <name>` case, the one
    # PEP 592 makes fail outright once every uploaded file is yanked.
    # Confirmed live with real pip 25.1.1 against a from-scratch local PEP
    # 503 index serving a single release with its only file marked
    # `data-yanked`: `pip install totally-yanked-test-pkg` (no version pin)
    # genuinely fails with "ERROR: Ignored the following yanked versions:
    # 1.0.0" / "Could not find a version that satisfies the requirement ...
    # (from versions: none)" / "No matching distribution found" -- the
    # identical failure shape pip gives for a name that doesn't exist at
    # all -- while `pip install totally-yanked-test-pkg==1.0.0` (exact pin)
    # still installs it, with a warning. A control package on the same
    # index with no yanked files installed normally either way, confirming
    # the index setup itself wasn't the cause. Since slopcheck can never see
    # or check a pin, "every uploaded file across every release is yanked"
    # is exactly the same as "not found" from its perspective -- the same
    # "registry still answers 200 for an entity real tooling treats as
    # gone" shape already fixed for npm's unpublished state, just PyPI's
    # own distinct mechanism for it.
    if all(file_info.get("yanked") for file_info in release_files):
        return LookupResult("not_found", "every release has been yanked on PyPI")

    # The age used for the "recent" heuristic has to reflect the oldest
    # release a real unqualified `pip install <name>` can actually resolve
    # to, not the oldest release ever uploaded. PEP 592 makes pip ignore
    # every yanked file when there's no exact version pin (the case this
    # tool always models -- see this function's own yanked-handling comment
    # above), so a yanked file's upload date shouldn't count toward "how
    # long has this been installable" any more than it counts toward
    # whether the project is installable at all (the all-yanked check just
    # above). Before this fix, `min(upload_times)` was computed across every
    # release file regardless of yanked status, so a project whose only old
    # release was yanked and whose sole real, installable release is brand
    # new reported "ok" (the old, yanked file's date dominated the min) even
    # though a real `pip install <name>` today resolves straight to the
    # fresh release and only the fresh release -- confirmed live (pip
    # 25.1.1, a from-scratch local index: `testpkg-1.0.0` marked
    # `data-yanked`, `testpkg-1.0.1` not): `pip install --dry-run -i <index>
    # testpkg` downloads and would install only 1.0.1, never considering
    # 1.0.0 at all. That's exactly the shape a reused/taken-over PyPI
    # project name can produce (old legitimate releases yanked, one new
    # possibly-malicious release published) and exactly the "a name that
    # looks like it's been here a while, but the thing you'd actually
    # install is brand new" case the "recent" flag exists to catch.
    non_yanked_upload_times = [
        file_info["upload_time_iso_8601"]
        for file_info in release_files
        if "upload_time_iso_8601" in file_info and not file_info.get("yanked")
    ]
    age_days = _days_since(min(non_yanked_upload_times or upload_times))
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

    # An unpublished package still has a registry document (HTTP 200, with
    # the original "time.created" intact) but real npm tooling treats it as
    # nonexistent: `npm install`/`npm view` return a hard 404 ("Unpublished
    # on <date>"), not the contents of the old publish. Without this check,
    # `created` below is read from that stale pre-unpublish timestamp, so a
    # package unpublished shortly after a fresh release reports "recent" and
    # one unpublished long after its original release reports "ok" — both
    # implying an installable package when npm can no longer install it at
    # all. Verified against the live registry (a same-day publish+unpublish
    # observed via the replication feed) and confirmed with the real `npm
    # view` client, which surfaces exactly this "404 Unpublished" error.
    if data.get("time", {}).get("unpublished"):
        return LookupResult("not_found", "package was unpublished from npm")

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
