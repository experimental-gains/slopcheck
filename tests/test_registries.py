from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from slopcheck import registries


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_check_pypi_not_found():
    with patch.object(registries, "_get_json", return_value=None):
        result = registries.check_pypi("this-package-does-not-exist-xyz")
    assert result.status == "not_found"


def test_check_pypi_ok_for_old_package():
    data = {
        "releases": {
            "1.0.0": [{"upload_time_iso_8601": _iso(400)}],
        }
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("requests")
    assert result.status == "ok"


def test_check_pypi_flags_recent_package():
    data = {"releases": {"0.0.1": [{"upload_time_iso_8601": _iso(2)}]}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("brand-new-thing")
    assert result.status == "recent"


def test_check_pypi_all_releases_yanked_is_not_found():
    # Real registry shape (captured live via https://pypi.org/pypi/tzdata/json,
    # which confirmed PyPI's JSON API carries a per-file "yanked" bool on
    # every release, old and new) for a package where every uploaded file of
    # every release has been yanked: real pip 25.1.1 against a from-scratch
    # local index reproducing this exact shape refuses an unqualified
    # `pip install <name>` outright ("No matching distribution found"), the
    # same failure shape as a name that was never published at all -- so
    # this must report not_found, not "ok" or "recent".
    data = {
        "releases": {
            "1.0.0": [
                {
                    "upload_time_iso_8601": _iso(400),
                    "yanked": True,
                    "yanked_reason": "critical bug",
                }
            ],
        }
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("totally-yanked-test-pkg")
    assert result.status == "not_found"
    assert result.detail == "every release has been yanked on PyPI"


def test_check_pypi_some_releases_yanked_is_not_flagged():
    # A package with a mix of yanked and non-yanked releases still has a
    # real installable candidate for an unqualified install (real pip picks
    # the newest non-yanked one) -- only *all* releases being yanked should
    # trigger the not_found downgrade.
    data = {
        "releases": {
            "1.0.0": [{"upload_time_iso_8601": _iso(400), "yanked": True}],
            "2.0.0": [{"upload_time_iso_8601": _iso(300), "yanked": False}],
        }
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("mostly-fine-package")
    assert result.status == "ok"


def test_check_pypi_all_yanked_but_recent_is_still_not_found():
    # Even a freshly (and fully) yanked package should report not_found,
    # not "recent" -- there is no installable candidate for an unqualified
    # install regardless of how new the yanked upload was.
    data = {
        "releases": {
            "0.0.1": [{"upload_time_iso_8601": _iso(2), "yanked": True}],
        }
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("brand-new-but-yanked")
    assert result.status == "not_found"


def test_check_pypi_recent_release_after_old_yanked_release_is_flagged_recent():
    # A project whose only old release was yanked and whose sole real,
    # installable release is brand new: a real unqualified `pip install
    # <name>` ignores the yanked file completely (PEP 592) and resolves
    # straight to the fresh one -- confirmed live (pip 25.1.1, a from-scratch
    # local index with testpkg-1.0.0 marked `data-yanked` and testpkg-1.0.1
    # not): `pip install --dry-run -i <index> testpkg` downloads and would
    # install only 1.0.1, never even considering 1.0.0. So the age that
    # matters for the "recent" heuristic is the fresh, non-yanked release's
    # age, not the long-dead yanked one's -- a hallucinated/reused project
    # name whose only old release was yanked and which just got a brand new
    # (possibly malicious) release should still be flagged "recent", the
    # same as any other package published days ago.
    data = {
        "releases": {
            "0.1.0": [{"upload_time_iso_8601": _iso(400), "yanked": True}],
            "1.0.0": [{"upload_time_iso_8601": _iso(2), "yanked": False}],
        }
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("old-yanked-then-fresh-takeover")
    assert result.status == "recent"


def test_check_npm_not_found():
    with patch.object(registries, "_get_json", return_value=None):
        result = registries.check_npm("this-package-does-not-exist-xyz")
    assert result.status == "not_found"


def test_check_npm_ok_for_old_package():
    data = {"time": {"created": _iso(1000)}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_npm("left-pad")
    assert result.status == "ok"


def test_check_npm_flags_recent_package():
    data = {"time": {"created": _iso(5)}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_npm("brand-new-thing")
    assert result.status == "recent"


def test_network_error_is_reported_not_raised():
    with patch.object(registries, "_get_json", side_effect=OSError("boom")):
        result = registries.check_pypi("requests")
    assert result.status == "error"


def test_check_pypi_error_detail_includes_exception_message():
    with patch.object(registries, "_get_json", side_effect=OSError("boom")):
        result = registries.check_pypi("requests")
    assert result.detail == "boom"


def test_check_pypi_not_found_detail():
    with patch.object(registries, "_get_json", return_value=None):
        result = registries.check_pypi("this-package-does-not-exist-xyz")
    assert result.detail == "no such project on PyPI"


def test_check_pypi_queries_exact_url_for_name():
    captured = {}

    def fake_get_json(url):
        captured["url"] = url
        return None

    with patch.object(registries, "_get_json", fake_get_json):
        registries.check_pypi("requests")

    assert captured["url"] == "https://pypi.org/pypi/requests/json"


def test_check_pypi_ok_when_data_has_no_releases_key():
    # A defensive default (`data.get("releases", {})`) that no existing test
    # actually exercised — every fixture always included a "releases" key.
    with patch.object(registries, "_get_json", return_value={}):
        result = registries.check_pypi("some-package")
    assert result.status == "ok"


def test_check_pypi_release_with_no_files_is_not_found():
    # Real-world find: `pypi.org/pypi/requests_extension/json` (a real,
    # currently-registered PyPI project -- confirmed live 2026-09-28)
    # returns HTTP 200 with `"releases": {"0.0.0": []}` -- a release
    # version is registered but carries zero uploaded files. Real pip
    # 25.1.1 fails an unqualified `pip install requests_extension` outright
    # ("Could not find a version that satisfies the requirement
    # requests_extension (from versions: none)" / "No matching distribution
    # found for requests_extension") -- the identical failure shape as a
    # name that was never registered at all. Before this fix, `release_files`
    # flattened to an empty list, so `upload_times` was also empty and the
    # pre-existing `if not upload_times: return LookupResult("ok")`
    # fallback fired first, reporting a genuinely uninstallable project as
    # "ok". This is deliberately distinct from
    # `test_check_pypi_ok_when_data_has_no_releases_key` above (`releases`
    # missing/empty entirely, a shape that doesn't appear reachable for a
    # real 200 response) -- here `releases` has a real version key, just an
    # empty file list under it.
    data = {"releases": {"0.0.0": []}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("requests_extension")
    assert result.status == "not_found"
    assert result.detail == "project has no installable release files on PyPI"


def test_check_pypi_release_with_no_files_among_others_still_checked():
    # A mix of an empty-file release version and a real installable one
    # must not trip the new not_found branch -- only *zero files across
    # every version* should.
    data = {
        "releases": {
            "0.0.0": [],
            "1.0.0": [{"upload_time_iso_8601": _iso(400)}],
        }
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("some-package")
    assert result.status == "ok"


def test_check_pypi_recent_detail_includes_age():
    data = {"releases": {"0.0.1": [{"upload_time_iso_8601": _iso(2)}]}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("brand-new-thing")
    assert result.detail == "first published 2 days ago"


def test_check_pypi_boundary_at_recent_days_is_not_recent(monkeypatch):
    monkeypatch.setattr(registries, "_days_since", lambda ts: 30.0)
    data = {"releases": {"0.0.1": [{"upload_time_iso_8601": "irrelevant"}]}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_pypi("some-package")
    assert result.status == "ok"


def test_check_npm_error_status_and_detail():
    with patch.object(registries, "_get_json", side_effect=OSError("boom")):
        result = registries.check_npm("left-pad")
    assert result.status == "error"
    assert result.detail == "boom"


def test_check_npm_not_found_detail():
    with patch.object(registries, "_get_json", return_value=None):
        result = registries.check_npm("this-package-does-not-exist-xyz")
    assert result.detail == "no such package on npm"


def test_check_npm_url_encodes_scoped_package_name():
    captured = {}

    def fake_get_json(url):
        captured["url"] = url
        return None

    with patch.object(registries, "_get_json", fake_get_json):
        registries.check_npm("@scope/pkg")

    # safe='' means the '/' inside a scoped name must be escaped too, not
    # left bare the way urllib.parse.quote's default safe='/' would leave it.
    assert captured["url"] == "https://registry.npmjs.org/%40scope%2Fpkg"


def test_check_npm_ok_when_data_has_no_time_key():
    with patch.object(registries, "_get_json", return_value={}):
        result = registries.check_npm("left-pad")
    assert result.status == "ok"


def test_check_npm_recent_detail_includes_age():
    data = {"time": {"created": _iso(5)}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_npm("brand-new-thing")
    assert result.detail == "first published 5 days ago"


def test_check_npm_unpublished_package_is_not_found():
    # Real registry shape for an unpublished npm package (captured live from
    # https://registry.npmjs.org/@jinyezhao/hyness-plugins, which npm's own
    # replication feed flagged `deleted` yet the registry still answers 200
    # with the pre-unpublish "time.created" and no "versions" key): the doc
    # keeps `created` from the original (recent) publish, but real npm
    # tooling (`npm view`) hard-fails with "404 Unpublished on <date>" for
    # this exact name -- it is not installable, so slopcheck must not call
    # it "recent" (implying merely new-but-real) or "ok".
    data = {
        "time": {
            "created": _iso(0.01),
            "modified": _iso(0.005),
            "1.0.0": _iso(0.01),
            "unpublished": {"time": _iso(0.005), "versions": ["1.0.0"]},
        },
        "maintainers": [{"email": "example@example.com", "name": "someone"}],
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_npm("@jinyezhao/hyness-plugins")
    assert result.status == "not_found"


def test_check_npm_unpublished_long_ago_is_not_found_not_ok():
    # Same unpublished state, but the original publish was long enough ago
    # that the old (pre-fix) code path would have hit the "ok" branch --
    # the most misleading divergence, since it silently vouches for a
    # dependency that `npm install` cannot actually fetch.
    data = {
        "time": {
            "created": _iso(1000),
            "modified": _iso(500),
            "unpublished": {"time": _iso(500), "versions": ["1.0.0"]},
        },
    }
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_npm("some-old-unpublished-package")
    assert result.status == "not_found"


def test_check_npm_boundary_at_recent_days_is_not_recent(monkeypatch):
    monkeypatch.setattr(registries, "_days_since", lambda ts: 30.0)
    data = {"time": {"created": "irrelevant"}}
    with patch.object(registries, "_get_json", return_value=data):
        result = registries.check_npm("some-package")
    assert result.status == "ok"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def test_get_json_sends_user_agent_and_timeout_and_parses_body():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(b'{"ok": true}')

    with patch.object(registries.urllib.request, "urlopen", fake_urlopen):
        result = registries._get_json("https://pypi.org/pypi/requests/json")

    assert result == {"ok": True}
    assert captured["request"].get_header("User-agent") == registries._USER_AGENT
    assert captured["timeout"] == registries._TIMEOUT


def _freeze_now(monkeypatch, frozen_now: datetime):
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now

    monkeypatch.setattr(registries, "datetime", _FrozenDatetime)


def test_days_since_handles_real_zulu_suffixed_timestamp(monkeypatch):
    # PyPI/npm both return upload/creation timestamps with a trailing "Z"
    # (Zulu/UTC), not a "+00:00" offset — this is the real wire format, not
    # just a theoretical one.
    _freeze_now(monkeypatch, datetime(2026, 1, 4, tzinfo=timezone.utc))

    age = registries._days_since("2026-01-01T00:00:00Z")

    assert age == 3.0


def test_days_since_divides_by_seconds_per_day(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 1, 11, tzinfo=timezone.utc))

    age = registries._days_since("2026-01-01T00:00:00+00:00")

    assert age == 10.0
