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


def test_main_errors_cleanly_on_malformed_package_json(tmp_path: Path, capsys):
    # Regression test for run #109: a corrupt/truncated package.json used to
    # raise an unhandled json.JSONDecodeError all the way out of main(),
    # printing a Python traceback instead of a clean slopcheck error.
    (tmp_path / "package.json").write_text("this is not json at all {{{")

    exit_code = cli.main([str(tmp_path)])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "package.json" in err
    assert "Traceback" not in err


def test_main_errors_cleanly_on_malformed_pyproject_toml(tmp_path: Path, capsys):
    (tmp_path / "pyproject.toml").write_text("[project\nname = \"broken\"\n")

    exit_code = cli.main([str(tmp_path)])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "pyproject.toml" in err
    assert "Traceback" not in err


def test_main_errors_cleanly_on_invalid_utf8_requirements_txt(tmp_path: Path, capsys):
    (tmp_path / "requirements.txt").write_bytes(b"requests==2.31.0\n\xff\xfe\x00\x81\nnumpy==1.26.0\n")

    exit_code = cli.main([str(tmp_path)])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "requirements.txt" in err
    assert "Traceback" not in err


def test_private_pypi_index_downgrades_not_found_to_private(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://pypi.internal.example/simple")
    (tmp_path / "requirements.txt").write_text("acmecorp-internal-widget\n")

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker(set())}):
        exit_code = cli.main([str(tmp_path)])

    assert exit_code == 0  # "private" doesn't fail the default --fail-on=not_found gate
    out = capsys.readouterr().out
    assert "PRIVATE" in out
    assert "acmecorp-internal-widget" in out


def test_npm_scoped_registry_only_exempts_that_scope(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    (tmp_path / ".npmrc").write_text("@acmecorp:registry=https://npm.internal.example/\n")
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@acmecorp/widget": "^1.0.0", "totally-made-up-pkg-9000": "^1.0.0"}}'
    )

    def fake_npm(name: str):
        return LookupResult("not_found", "no such package")

    with patch.dict(cli.CHECKERS, {"npm": fake_npm}):
        exit_code = cli.main([str(tmp_path)])

    assert exit_code == 1  # the unrelated hallucinated dep still fails the build
    results = {dep.name: result.status for dep, result in cli.scan(cli.find_manifests(tmp_path))}
    assert results["@acmecorp/widget"] == "private"
    assert results["totally-made-up-pkg-9000"] == "not_found"


def test_json_output_is_valid(tmp_path: Path, capsys):
    (tmp_path / "requirements.txt").write_text("requests\n")
    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        cli.main([str(tmp_path), "--json"])
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {"name": "requests", "ecosystem": "pypi", "source": str(tmp_path / "requirements.txt"), "status": "ok", "detail": ""}
    ]
