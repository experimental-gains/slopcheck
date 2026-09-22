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
