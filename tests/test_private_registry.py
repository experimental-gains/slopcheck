from pathlib import Path

from slopcheck import private_registry
from slopcheck.private_registry import (
    npm_private_registry_context,
    npm_scope,
    pip_private_index_configured,
)


def test_pip_no_private_index_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is False


def test_pip_private_index_from_env_var(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://pypi.internal.example/simple")
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_requirements_txt_directive(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("--extra-index-url https://pypi.internal.example/simple\nrequests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_requirements_txt_short_flag(tmp_path: Path, monkeypatch):
    """pip's `-i` is the registered short alias for `--index-url` (confirmed
    against pip's own `cmdoptions.index_url` short_opts and by parsing this
    exact line with pip's real `parse_requirements()`); a common real-world
    way to point requirements.txt at an internal artifact registry."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("-i https://pypi.internal.example/simple\nrequests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_requirements_txt_short_flag_attached(tmp_path: Path, monkeypatch):
    """pip also accepts the short flag with no space before its value
    (optparse short-option attachment, e.g. `-ihttps://...`)."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("-ihttps://pypi.internal.example/simple\nrequests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_pip_conf(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    pip_conf = tmp_path / "pip.conf"
    pip_conf.write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_conf))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_virtualenv_pip_conf(tmp_path: Path, monkeypatch):
    # pip reads $VIRTUAL_ENV/pip.conf when running inside an active venv, in
    # addition to the user/system locations — verified live against pip's
    # own docs and behavior. A mutation-testing pass (mutmut, run #126)
    # found every existing test explicitly unset VIRTUAL_ENV to isolate from
    # the real dev environment, but none of them set it to confirm this
    # venv-local config location is actually read.
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    pip_conf = venv_dir / "pip.conf"
    pip_conf.write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_npm_no_private_registry_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == set()


def test_npm_scope_registry_from_npmrc(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".npmrc").write_text("@acmecorp:registry=https://npm.internal.example/\n")

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_npm_blanket_registry_from_env_var(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("npm_config_registry", "https://npm.internal.example/")

    blanket, _scopes = npm_private_registry_context([tmp_path])

    assert blanket is True


def test_npm_scope_helper():
    assert npm_scope("@acmecorp/widget") == "@acmecorp"
    assert npm_scope("requests") is None
    assert npm_scope("@acmecorp") is None


def test_npm_scope_uses_first_slash_not_last():
    # split(..., 1) vs rsplit(..., 1) only diverge when there's more than
    # one "/" — no existing fixture had a second slash to force that.
    assert npm_scope("@acmecorp/widget/sub") == "@acmecorp"


def test_pip_config_paths_returns_known_locations_in_order(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._pip_config_paths() == [
        tmp_path / ".config" / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        Path("/etc/pip.conf"),
    ]


def test_pip_config_has_extra_index_checks_install_section(tmp_path: Path, monkeypatch):
    conf = tmp_path / "pip.conf"
    conf.write_text("[install]\nextra-index-url = https://pypi.internal.example/simple\n")
    monkeypatch.setattr(private_registry, "_pip_config_paths", lambda: [conf])

    assert private_registry._pip_config_has_extra_index() is True


def test_pip_config_has_extra_index_checks_bare_index_url_option(tmp_path: Path, monkeypatch):
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nindex-url = https://pypi.internal.example/simple\n")
    monkeypatch.setattr(private_registry, "_pip_config_paths", lambda: [conf])

    assert private_registry._pip_config_has_extra_index() is True


def test_pip_config_has_extra_index_skips_missing_file_and_checks_next(tmp_path: Path, monkeypatch):
    missing = tmp_path / "does-not-exist.conf"
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")
    monkeypatch.setattr(private_registry, "_pip_config_paths", lambda: [missing, conf])

    assert private_registry._pip_config_has_extra_index() is True


def test_pip_config_has_extra_index_skips_malformed_file_and_checks_next(tmp_path: Path, monkeypatch):
    malformed = tmp_path / "malformed.conf"
    malformed.write_text("not valid ini content\n")
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")
    monkeypatch.setattr(private_registry, "_pip_config_paths", lambda: [malformed, conf])

    assert private_registry._pip_config_has_extra_index() is True


def test_pip_private_index_from_pip_index_url_env_var(monkeypatch, tmp_path: Path):
    # PIP_INDEX_URL (replaces the default index entirely) is a distinct env
    # var from PIP_EXTRA_INDEX_URL and no existing test set it alone.
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.internal.example/simple")
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("requests>=2.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_skips_missing_requirements_file_and_checks_next(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    missing = tmp_path / "does-not-exist.txt"
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("--extra-index-url https://pypi.internal.example/simple\nrequests>=2.0\n")

    assert pip_private_index_configured([missing, reqs]) is True


def test_npm_uppercase_env_var_triggers_blanket(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://npm.internal.example/")

    blanket, _scopes = npm_private_registry_context([tmp_path])

    assert blanket is True


def test_npmrc_blanket_registry_line(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".npmrc").write_text("registry=https://npm.internal.example/\n")

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is True
    assert scopes == set()


def test_npmrc_skips_blank_and_comment_lines_but_reads_directive_after_them(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".npmrc").write_text(
        "\n# a hash comment\n; a semicolon comment\n@acmecorp:registry=https://npm.internal.example/\n"
    )

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_npmrc_paths_skips_missing_project_root_file_and_checks_home(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".npmrc").write_text("@acmecorp:registry=https://npm.internal.example/\n")
    project_root = tmp_path / "project"
    project_root.mkdir()

    _blanket, scopes = npm_private_registry_context([project_root])

    assert scopes == {"@acmecorp"}


def test_npmrc_paths_uses_dotfile_name(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._npmrc_paths([]) == [tmp_path / ".npmrc"]
