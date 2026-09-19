from pathlib import Path
from unittest.mock import patch

from slopcheck import cli
from slopcheck.registries import LookupResult


def _fake_checker(existing: set[str]):
    def check(name: str) -> LookupResult:
        if name in existing:
            return LookupResult("ok")
        return LookupResult("not_found", "no such project")

    return check


def test_main_exits_nonzero_on_hallucinated_dependency(tmp_path: Path, capsys):
    (tmp_path / "requirements.txt").write_text("requests\ntotally-made-up-pkg-9000\n")

    with patch.dict(
        cli.CHECKERS,
        {"pypi": _fake_checker({"requests"})},
    ):
        exit_code = cli.main([str(tmp_path)])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "totally-made-up-pkg-9000" in out
    assert "requests" not in out.split("\n")[1]  # clean dep isn't listed in the flagged rows


def test_main_exits_zero_when_everything_resolves(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("requests\n")

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        exit_code = cli.main([str(tmp_path)])

    assert exit_code == 0


def test_main_errors_on_missing_path(tmp_path: Path):
    missing = tmp_path / "nope"
    assert cli.main([str(missing)]) == 2


def test_main_errors_when_no_manifests_found(tmp_path: Path):
    assert cli.main([str(tmp_path)]) == 2


def test_json_output_is_valid(tmp_path: Path, capsys):
    (tmp_path / "requirements.txt").write_text("requests\n")
    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        cli.main([str(tmp_path), "--json"])
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {"name": "requests", "ecosystem": "pypi", "source": str(tmp_path / "requirements.txt"), "status": "ok", "detail": ""}
    ]
