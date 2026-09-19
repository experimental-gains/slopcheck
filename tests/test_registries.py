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
