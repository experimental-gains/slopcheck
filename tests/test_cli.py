import json
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_dedup_is_case_insensitive_across_files(tmp_path: Path):
    # Regression test for a mutmut survivor: the de-dupe key used
    # `dep.name.lower()`, mutated to `.upper()` — both normalize case
    # consistently for same-case duplicates, so a *mixed*-case duplicate
    # across two files is needed to actually distinguish them.
    (tmp_path / "requirements.txt").write_text("Requests\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "requirements.txt").write_text("requests\n")

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        results = cli.scan(cli.find_manifests(tmp_path))

    assert len(results) == 1


def test_scan_sorts_by_severity_not_alphabetically(tmp_path: Path):
    # Regression test for a mutmut survivor: `results.sort(key=...)`
    # mutated to `sort(key=None)`. Since manifest de-duping keeps
    # `Dependency` names unique, that mutant doesn't crash (there's
    # nothing to fall back to comparing `LookupResult` on) — it just
    # falls back to `Dependency`'s own tuple ordering, which happens to
    # equal severity order whenever names are already alphabetical. Pick
    # names whose alphabetical order is the *reverse* of severity order
    # to actually distinguish the two.
    (tmp_path / "requirements.txt").write_text("aaa-ok-pkg\nzzz-not-found-pkg\n")

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"aaa-ok-pkg"})}):
        results = cli.scan(cli.find_manifests(tmp_path))

    assert [dep.name for dep, _ in results] == ["zzz-not-found-pkg", "aaa-ok-pkg"]


def test_pip_private_only_triggered_by_a_requirements_txt_directive_line(tmp_path: Path, monkeypatch):
    # Regression test for mutmut survivors that mutated the `p.name ==
    # "requirements.txt"` filter (to `!=`, or to a wrong-case string) —
    # earlier tests only exercised this via env vars, which bypass the
    # path filter entirely (pip_private_index_configured checks env vars
    # unconditionally, regardless of what paths are passed in).
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "requirements.txt").write_text("--extra-index-url https://pypi.internal.example/simple\nacmecorp-internal-widget\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"left-pad": "^1.0.0"}}')

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker(set()), "npm": _fake_checker({"left-pad"})}):
        results = cli.scan(cli.find_manifests(tmp_path))

    by_name = {dep.name: result.status for dep, result in results}
    assert by_name["acmecorp-internal-widget"] == "private"


def test_npm_blanket_registry_downgrades_unscoped_package(tmp_path: Path, monkeypatch):
    # Regression test for a mutmut survivor: `scan()` passing `None`
    # instead of the real `npm_blanket` value to `_downgrade_if_private`
    # went unnoticed because the only existing npm test used a *scoped*
    # override, never a blanket one.
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    (tmp_path / ".npmrc").write_text("registry=https://npm.internal.example/\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"totally-made-up-pkg-9000": "^1.0.0"}}')

    with patch.dict(cli.CHECKERS, {"npm": _fake_checker(set())}):
        results = cli.scan(cli.find_manifests(tmp_path))

    assert results[0][1].status == "private"


def test_print_report_labels_and_detail_formatting(capsys):
    # Regression test for a large family of mutmut survivors in
    # `_print_report`'s `labels` dict and detail-string formatting.
    # Substring checks (e.g. `"PRIVATE" in out`) can't catch mutmut's
    # "XX...XX"-wrap or single-character-case string mutants, since the
    # original text is still a substring of the mutated one (or differs
    # only in an ANSI escape byte a plain substring check skips over) —
    # this needs an exact full-output comparison. Also covers a
    # `continue`-to-`break` mutant (an "ok" row isn't the last row here,
    # so a `break` would silently drop everything after it) and an
    # always-true-detail-condition mutant (the last row has an empty
    # detail and must render with no "(...)" at all).
    from slopcheck.parsers import Dependency

    results = [
        (Dependency("pkg-a", "pypi", "req.txt"), LookupResult("not_found", "no such project")),
        (Dependency("pkg-b", "npm", "pkg.json"), LookupResult("recent", "first published 2 days ago")),
        (Dependency("pkg-c", "pypi", "req.txt"), LookupResult("error", "boom")),
        (Dependency("pkg-d", "pypi", "req.txt"), LookupResult("private", cli._UNVERIFIED_DETAIL)),
        (Dependency("pkg-e", "pypi", "req.txt"), LookupResult("ok")),
        (Dependency("pkg-f", "pypi", "req.txt"), LookupResult("not_found", "")),
    ]

    cli._print_report(results)
    out = capsys.readouterr().out

    expected = "\n".join(
        [
            "  \033[31mNOT FOUND\033[0m pypi  pkg-a (no such project) [req.txt]",
            "  \033[33mRECENT   \033[0m npm   pkg-b (first published 2 days ago) [pkg.json]",
            "  \033[33mERROR    \033[0m pypi  pkg-c (boom) [req.txt]",
            f"  \033[36mPRIVATE  \033[0m pypi  pkg-d ({cli._UNVERIFIED_DETAIL}) [req.txt]",
            "  \033[31mNOT FOUND\033[0m pypi  pkg-f [req.txt]",
            "",
        ]
    )
    assert out == expected


def test_help_text_matches_source(capsys):
    # Regression test for the large family of mutmut survivors mutating
    # `prog=`, `description=`, and the various `help=` strings passed to
    # argparse — none of that text is observable except through --help.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    assert out.startswith("usage: slopcheck ")
    # argparse wraps help text to terminal width, so line breaks aren't stable
    # across environments — compare on whitespace-collapsed text instead.
    normalized = " ".join(out.split())
    assert "Check whether every dependency in your manifests actually exists in its" in normalized
    assert "registry, and flag suspiciously new packages. Catches hallucinated" in normalized
    assert "('slopsquatted') package names before you install them." in normalized
    assert "Manifest files to check, or directories to search" in normalized
    assert "(requirements.txt, pyproject.toml, package.json). Defaults to the current directory." in normalized
    assert "Emit machine-readable JSON instead of text." in normalized
    assert "Minimum severity that causes a non-zero exit code (default: not_found)." in normalized
    # A plain `in` check can't tell "the real text" from "the real text with
    # extra padding wrapped around it" (mutmut's own "XX...XX"-wrap mutants
    # for exactly this reason) — the source text never contains a literal
    # "XX", so this closes that whole class in one assertion.
    assert "XX" not in normalized


def test_fail_on_rejects_an_invalid_choice():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--fail-on", "bogus"])
    assert exc_info.value.code == 2


def test_fail_on_accepts_all_three_documented_values(tmp_path: Path):
    # Regression test for mutmut survivors that mangled one of the three
    # `choices=[...]` strings (e.g. "not_found" -> "XXnot_foundXX" or
    # "NOT_FOUND"). Actually passing each value end-to-end and confirming
    # it's accepted is more direct than parsing it back out of `--help`.
    (tmp_path / "requirements.txt").write_text("requests\n")
    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        for value in ("not_found", "recent", "never"):
            assert cli.main([str(tmp_path), "--fail-on", value]) == 0


def test_main_defaults_to_current_directory_when_no_paths_given(tmp_path: Path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests\n")
    monkeypatch.chdir(tmp_path)

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        exit_code = cli.main([])

    assert exit_code == 0


def test_main_accepts_a_direct_file_path(tmp_path: Path):
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests\n")

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        exit_code = cli.main([str(manifest)])

    assert exit_code == 0


def test_missing_path_error_message(tmp_path: Path, capsys):
    missing = tmp_path / "nope"
    cli.main([str(missing)])
    err = capsys.readouterr().err
    assert f"slopcheck: no such file or directory: {missing}" in err


def test_no_manifests_error_message(tmp_path: Path, capsys):
    cli.main([str(tmp_path)])
    err = capsys.readouterr().err
    # Exact match, not `in`: a plain substring check can't distinguish this
    # from mutmut's "XX...XX"-wrapped version of the same literal, which
    # still contains the real text as a substring.
    assert err == "slopcheck: no requirements.txt, pyproject.toml, or package.json found\n"


def test_summary_messages_are_exact(tmp_path: Path, capsys):
    (tmp_path / "requirements.txt").write_text("requests\ntotally-made-up-pkg-9000\n")
    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        cli.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "slopcheck: 1 of 2 dependencies flagged" in out

    (tmp_path / "requirements.txt").write_text("requests\n")
    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker({"requests"})}):
        cli.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "slopcheck: 1 dependencies checked, all clean" in out


def test_fail_on_never_always_exits_zero_even_when_flagged(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("totally-made-up-pkg-9000\n")
    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker(set())}):
        exit_code = cli.main([str(tmp_path), "--fail-on", "never"])
    assert exit_code == 0


def test_fail_on_recent_treats_recent_as_failing(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("some-new-pkg\n")

    def fake_checker(name: str) -> LookupResult:
        return LookupResult("recent", "first published 1 days ago")

    with patch.dict(cli.CHECKERS, {"pypi": fake_checker}):
        exit_code = cli.main([str(tmp_path), "--fail-on", "recent"])
    assert exit_code == 1


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
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert payload == [
        {"name": "requests", "ecosystem": "pypi", "source": str(tmp_path / "requirements.txt"), "status": "ok", "detail": ""}
    ]
    # Regression test for mutmut survivor: `json.dumps(..., indent=2)` mutated to
    # `indent=None`/`indent=3` still produces valid, equal-after-parsing JSON, so
    # `json.loads` alone can't distinguish them — assert the actual indentation.
    assert out.startswith("[\n  {\n    ")


def test_downgrade_detail_text_is_exact_for_pypi_and_npm(tmp_path: Path, monkeypatch):
    # Regression test for mutmut survivors that dropped `_UNVERIFIED_DETAIL`
    # (replaced it with `None`) on both the pypi and npm downgrade branches —
    # prior tests only asserted the "private" status, never the detail text.
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://pypi.internal.example/simple")
    monkeypatch.setenv("npm_config_registry", "https://npm.internal.example/")
    (tmp_path / "requirements.txt").write_text("acmecorp-internal-widget\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"acmecorp-internal-widget": "^1.0.0"}}')

    with patch.dict(cli.CHECKERS, {"pypi": _fake_checker(set()), "npm": _fake_checker(set())}):
        results = cli.scan(cli.find_manifests(tmp_path))

    by_ecosystem = {dep.ecosystem: result for dep, result in results}
    assert by_ecosystem["pypi"].detail == cli._UNVERIFIED_DETAIL
    assert by_ecosystem["npm"].detail == cli._UNVERIFIED_DETAIL
