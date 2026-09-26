from pathlib import Path

from slopcheck import private_registry
from slopcheck.private_registry import (
    npm_private_registry_context,
    npm_scope,
    pip_private_index_configured,
    poetry_private_registry_context,
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
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._pip_config_paths() == [
        tmp_path / ".config" / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        Path("/etc/pip.conf"),
    ]


def test_pip_config_paths_uses_xdg_config_home_when_set(tmp_path: Path, monkeypatch):
    """Confirmed live against real pip (`pip config -v list` with
    XDG_CONFIG_HOME set): pip's own per-user config lookup moves to
    `$XDG_CONFIG_HOME/pip/pip.conf` instead of `~/.config/pip/pip.conf` —
    it's a replacement, not an addition, matching pip's vendored
    platformdirs.unix.Unix.user_config_dir implementation exactly."""
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg = tmp_path / "customxdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert private_registry._pip_config_paths() == [
        xdg / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        Path("/etc/pip.conf"),
    ]


def test_pip_config_paths_ignores_blank_xdg_config_home(tmp_path: Path, monkeypatch):
    """Real pip's platformdirs check is `if not path.strip()`, so an XDG_CONFIG_HOME
    set to the empty string (a real shell footgun: `export XDG_CONFIG_HOME=`) falls
    back to `~/.config`, the same as when it's unset entirely."""
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    assert private_registry._pip_config_paths()[0] == tmp_path / ".config" / "pip" / "pip.conf"


def test_pip_private_index_from_xdg_config_home_pip_conf(tmp_path: Path, monkeypatch):
    """End-to-end regression for the XDG_CONFIG_HOME gap: a private index
    configured only via `$XDG_CONFIG_HOME/pip/pip.conf` must be detected,
    since a real `pip install` in that exact environment resolves it fine
    (see _pip_user_config_dir's doc comment) — before the fix, this always
    returned False, and the caller (cli._downgrade_if_private) would report
    a legitimately-installable private-only dependency as a plain
    `not_found` hallucination instead of `private`/unverified."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg = tmp_path / "customxdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    conf_dir = xdg / "pip"
    conf_dir.mkdir(parents=True)
    (conf_dir / "pip.conf").write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")

    assert pip_private_index_configured([]) is True


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


_DEFAULT_GLOBAL_NPMRC_PATHS = [Path("/etc/npmrc"), Path("/usr/local/etc/npmrc")]


def test_npmrc_paths_uses_dotfile_name(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_userconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    monkeypatch.delenv("npm_config_globalconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_GLOBALCONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._npmrc_paths([]) == [tmp_path / ".npmrc", *_DEFAULT_GLOBAL_NPMRC_PATHS]


def test_npmrc_paths_uses_userconfig_env_var_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    monkeypatch.delenv("npm_config_globalconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_GLOBALCONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    custom = tmp_path / "custom.npmrc"
    monkeypatch.setenv("npm_config_userconfig", str(custom))

    assert private_registry._npmrc_paths([]) == [custom, *_DEFAULT_GLOBAL_NPMRC_PATHS]


def test_npmrc_paths_uses_uppercase_userconfig_env_var_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_userconfig", raising=False)
    monkeypatch.delenv("npm_config_globalconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_GLOBALCONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    custom = tmp_path / "custom.npmrc"
    monkeypatch.setenv("NPM_CONFIG_USERCONFIG", str(custom))

    assert private_registry._npmrc_paths([]) == [custom, *_DEFAULT_GLOBAL_NPMRC_PATHS]


def test_npmrc_paths_uses_globalconfig_env_var_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_userconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    monkeypatch.delenv("NPM_CONFIG_GLOBALCONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    custom_global = tmp_path / "custom-global.npmrc"
    monkeypatch.setenv("npm_config_globalconfig", str(custom_global))

    assert private_registry._npmrc_paths([]) == [tmp_path / ".npmrc", custom_global]


def test_npmrc_paths_uses_uppercase_globalconfig_env_var_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("npm_config_userconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    monkeypatch.delenv("npm_config_globalconfig", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    custom_global = tmp_path / "custom-global.npmrc"
    monkeypatch.setenv("NPM_CONFIG_GLOBALCONFIG", str(custom_global))

    assert private_registry._npmrc_paths([]) == [tmp_path / ".npmrc", custom_global]


def test_npmrc_paths_uses_mixedcase_userconfig_env_var_override(tmp_path: Path, monkeypatch):
    """Real npm matches its env-derived config keys fully case-insensitively,
    not just the plain-lowercase and SCREAMING_CASE spellings the two tests
    above already cover — confirmed live (`Npm_Config_Userconfig=<file> npm
    config get <key>` genuinely read that file). Before this fix, only those
    two exact spellings were checked, so a mixed-case spelling (plausible
    wherever env vars pass through case-normalizing tooling, e.g. a Windows
    host, where env var names are inherently case-insensitive) was silently
    ignored, sending the scan to the default `~/.npmrc` instead of the
    relocated file a real `npm install` would actually consult."""
    monkeypatch.delenv("npm_config_userconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    monkeypatch.delenv("npm_config_globalconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_GLOBALCONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    custom = tmp_path / "custom.npmrc"
    monkeypatch.setenv("Npm_Config_Userconfig", str(custom))

    assert private_registry._npmrc_paths([]) == [custom, *_DEFAULT_GLOBAL_NPMRC_PATHS]


def test_npmrc_paths_uses_mixedcase_globalconfig_env_var_override(tmp_path: Path, monkeypatch):
    """Same case-insensitivity gap, for `globalconfig` instead of
    `userconfig` — confirmed live the same way."""
    monkeypatch.delenv("npm_config_userconfig", raising=False)
    monkeypatch.delenv("NPM_CONFIG_USERCONFIG", raising=False)
    monkeypatch.delenv("npm_config_globalconfig", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    custom_global = tmp_path / "custom-global.npmrc"
    monkeypatch.setenv("Npm_Config_Globalconfig", str(custom_global))

    assert private_registry._npmrc_paths([]) == [tmp_path / ".npmrc", custom_global]


def test_npm_private_registry_from_global_npmrc_scope(tmp_path: Path, monkeypatch):
    """End-to-end regression for the missing-global-npmrc gap: a scope
    mapping configured only via npm's global config file must be detected,
    since real npm (confirmed live: `npm_config_globalconfig=<file> npm
    config get <scope>:registry`) resolves it from exactly that file even
    when no project or user npmrc mentions the scope at all — a real
    pattern for an org baking a private-registry mapping into a CI runner
    or Docker base image at the machine level. Before the fix, this always
    missed the scope and would report a legitimately-installable
    private-only dependency as a plain `not_found` hallucination instead
    of `private`/unverified."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    global_conf = tmp_path / "global.npmrc"
    global_conf.write_text("@acmecorp:registry=https://npm.internal.example/\n")
    monkeypatch.setenv("npm_config_globalconfig", str(global_conf))

    blanket, scopes = npm_private_registry_context([])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_npm_private_registry_from_global_npmrc_blanket(tmp_path: Path, monkeypatch):
    """Same gap, blanket form: an org-wide mirror/proxy configured only via
    the global npmrc's `registry=` line (no env var, no project/user
    npmrc) is a real, arguably more common enterprise pattern than a
    scope mapping (Artifactory/Nexus/Verdaccio routing *every* npm
    install through an internal proxy) — confirmed live the same way.
    Before the fix this was invisible, so every dependency in the project
    would be checked against the wrong (public) registry."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    global_conf = tmp_path / "global.npmrc"
    global_conf.write_text("registry=https://npm.internal.example/\n")
    monkeypatch.setenv("npm_config_globalconfig", str(global_conf))

    blanket, _scopes = npm_private_registry_context([])

    assert blanket is True


def test_npm_private_registry_from_relocated_userconfig(tmp_path: Path, monkeypatch):
    """End-to-end regression for the npm_config_userconfig gap: a scope
    mapping configured only via a relocated user config must be detected,
    since real npm (confirmed live) resolves it from exactly that file
    instead of ~/.npmrc when the env var is set — before the fix, this
    always missed the scope and would report a legitimately-installable
    private-only dependency as a plain `not_found` hallucination instead
    of `private`/unverified."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    custom = tmp_path / "custom.npmrc"
    custom.write_text("@acmecorp:registry=https://npm.internal.example/\n")
    monkeypatch.setenv("npm_config_userconfig", str(custom))

    blanket, scopes = npm_private_registry_context([])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_npm_private_registry_blanket_env_var_mixed_case(monkeypatch):
    """Same case-insensitivity gap as the userconfig/globalconfig ones
    above, for the blanket `npm_config_registry` env var itself — confirmed
    live the same way (`Npm_Config_Registry=... npm config get registry`
    genuinely returned the configured value). Before this fix, only the
    plain-lowercase and SCREAMING_CASE spellings were checked, so this
    would have wrongly reported no private registry configured."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.delenv("YARN_NPM_REGISTRY_SERVER", raising=False)
    monkeypatch.setenv("Npm_Config_Registry", "https://npm.internal.example/")

    blanket, _scopes = npm_private_registry_context([])

    assert blanket is True


def _clear_registry_env(monkeypatch):
    for var in (
        "npm_config_registry",
        "NPM_CONFIG_REGISTRY",
        "YARN_NPM_REGISTRY_SERVER",
        "YARN_RC_FILENAME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_yarnrc_scope_registry(tmp_path: Path, monkeypatch):
    """End-to-end regression: Yarn Berry (v2+) doesn't read `.npmrc` at all —
    its own `.yarnrc.yml` is a separate config file entirely invisible to
    the `.npmrc`-only detection above. Confirmed live with a real `yarn
    install`: a scope routed to a private registry only via
    `npmScopes.<name>.npmRegistryServer` in `.yarnrc.yml` is genuinely
    honored (the resolution step visibly tried the configured address).
    Before this fix, a legitimately-installable Yarn-Berry-private
    dependency was reported as a plain `not_found` hallucination."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc.yml").write_text(
        "nodeLinker: node-modules\n"
        "npmScopes:\n"
        "  acmecorp:\n"
        '    npmRegistryServer: "https://npm.internal.example/"\n'
    )

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_yarnrc_blanket_registry(tmp_path: Path, monkeypatch):
    """Same gap, blanket form: confirmed live that a top-level
    `npmRegistryServer:` in `.yarnrc.yml` routes every package through it,
    the Yarn-Berry analog of npm's `registry=` line."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc.yml").write_text('npmRegistryServer: "https://npm.internal.example/"\n')

    blanket, _scopes = npm_private_registry_context([tmp_path])

    assert blanket is True


def test_yarnrc_env_var_triggers_blanket(tmp_path: Path, monkeypatch):
    """`YARN_NPM_REGISTRY_SERVER` is the env var equivalent of the blanket
    `.yarnrc.yml` form — confirmed live (a real `yarn install` attempted to
    connect to the address it named, with no `.yarnrc.yml` involved)."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("YARN_NPM_REGISTRY_SERVER", "https://npm.internal.example/")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, _scopes = npm_private_registry_context([])

    assert blanket is True


def test_yarnrc_home_global_config_is_read(tmp_path: Path, monkeypatch):
    """Yarn Berry merges a home-directory `~/.yarnrc.yml` in as a global
    config, the same way npm has a separate per-user npmrc — confirmed live:
    a scope mapping placed only there (no project-level `.yarnrc.yml` at
    all) was genuinely honored by a real `yarn install`."""
    _clear_registry_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".yarnrc.yml").write_text(
        "npmScopes:\n  acmecorp:\n    npmRegistryServer: https://npm.internal.example/\n"
    )
    project_root = tmp_path / "project"
    project_root.mkdir()

    _blanket, scopes = npm_private_registry_context([project_root])

    assert scopes == {"@acmecorp"}


def test_yarnrc_filename_env_var_override(tmp_path: Path, monkeypatch):
    """`YARN_RC_FILENAME` relocates which per-directory config file Yarn
    Berry reads (project root and the home-directory global one alike) —
    confirmed live (`YARN_RC_FILENAME=custom.yarnrc.yml yarn install`
    genuinely read that file instead of the default `.yarnrc.yml`)."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    monkeypatch.setenv("YARN_RC_FILENAME", "custom.yarnrc.yml")
    (tmp_path / "custom.yarnrc.yml").write_text(
        "npmScopes:\n  acmecorp:\n    npmRegistryServer: https://npm.internal.example/\n"
    )
    # The default filename must NOT be consulted once relocated.
    (tmp_path / ".yarnrc.yml").write_text("npmScopes:\n  wrongcorp:\n    npmRegistryServer: https://x/\n")

    _blanket, scopes = npm_private_registry_context([tmp_path])

    assert scopes == {"@acmecorp"}


def test_yarnrc_filename_env_var_override_mixed_case(tmp_path: Path, monkeypatch):
    """Same case-insensitivity gap as npm's env vars, for Yarn Berry's own
    `YARN_RC_FILENAME` — confirmed live (`Yarn_Rc_Filename=custom.yarnrc.yml
    yarn install` genuinely read the relocated file). Before this fix, only
    the exact `YARN_RC_FILENAME` spelling was checked, so this would have
    silently fallen back to the default `.yarnrc.yml` instead."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    monkeypatch.setenv("Yarn_Rc_Filename", "custom.yarnrc.yml")
    (tmp_path / "custom.yarnrc.yml").write_text(
        "npmScopes:\n  acmecorp:\n    npmRegistryServer: https://npm.internal.example/\n"
    )

    _blanket, scopes = npm_private_registry_context([tmp_path])

    assert scopes == {"@acmecorp"}


def test_yarnrc_env_var_triggers_blanket_mixed_case(tmp_path: Path, monkeypatch):
    """Same gap for `YARN_NPM_REGISTRY_SERVER` — confirmed live
    (`Yarn_Npm_Registry_Server=... yarn config get npmRegistryServer`
    genuinely returned the configured value)."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("Yarn_Npm_Registry_Server", "https://npm.internal.example/")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, _scopes = npm_private_registry_context([])

    assert blanket is True


def test_yarnrc_ignores_nested_key_that_is_not_npm_registry_server(tmp_path: Path, monkeypatch):
    """A scope block with other keys (e.g. `npmAuthToken`, a real Yarn
    Berry key for private-registry auth) but no `npmRegistryServer` isn't
    itself evidence of a *different* registry — only presence of the
    registry-server key should downgrade a name to `private`."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc.yml").write_text(
        "npmScopes:\n  acmecorp:\n    npmAuthToken: some-token\n"
    )

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == set()


def test_yarnrc_missing_file_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == set()


def test_poetry_no_source_table_is_not_private(tmp_path: Path):
    """No `[[tool.poetry.source]]` at all: pure PyPI, matching the
    no-config default for pip/npm above."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.poetry.dependencies]\n"
        'requests = "^2.0"\n'
    )

    blanket, explicit_names = poetry_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_poetry_source_with_no_priority_is_blanket(tmp_path: Path):
    """A source with no `priority` key defaults to `primary` and real
    `poetry lock` deactivates PyPI entirely in favor of it -- confirmed
    live ("Adding repository ... and setting it as primary. Deactivating
    the PyPI repository."). Every pypi dependency in this file should be
    treated the same as pip's blanket extra-index-url case."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[[tool.poetry.source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple/"\n'
    )

    blanket, _explicit_names = poetry_private_registry_context([pyproject])

    assert blanket is True


def test_poetry_supplemental_source_is_blanket(tmp_path: Path):
    """`priority = "supplemental"` still gets consulted for any name PyPI
    doesn't have -- confirmed live (PyPI 404'd first, then the
    supplemental source was tried regardless of any per-dependency
    `source =` reference)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[[tool.poetry.source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple/"\n'
        'priority = "supplemental"\n'
    )

    blanket, _explicit_names = poetry_private_registry_context([pyproject])

    assert blanket is True


def test_poetry_explicit_source_is_scoped_not_blanket(tmp_path: Path):
    """`priority = "explicit"` is never consulted for a dependency that
    doesn't opt in -- confirmed live (an unreferenced explicit source
    left the fake dependency a plain PyPI-only `SolverProblemError`,
    `127.0.0.1:9` never contacted at all)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[[tool.poetry.source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple/"\n'
        'priority = "explicit"\n'
        "\n"
        "[tool.poetry.dependencies]\n"
        'unrelated-pkg = "^1.0"\n'
    )

    blanket, explicit_names = poetry_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_poetry_dependency_pinned_to_explicit_source_is_scoped_private(tmp_path: Path):
    """A dependency that references an explicit source by name (`source =
    "internal"`) genuinely resolves only against it -- confirmed live
    (went straight to the private URL, PyPI never contacted)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[[tool.poetry.source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple/"\n'
        'priority = "explicit"\n'
        "\n"
        "[tool.poetry.dependencies]\n"
        'internal-only-pkg = { version = "^1.0", source = "internal" }\n'
        'public-pkg = "^2.0"\n'
    )

    blanket, explicit_names = poetry_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == {"internal-only-pkg"}


def test_poetry_explicit_source_scoping_covers_dependency_groups(tmp_path: Path):
    """The same explicit-source scoping applies inside `[tool.poetry.
    group.<name>.dependencies]`, not just the top-level table."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[[tool.poetry.source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple/"\n'
        'priority = "explicit"\n'
        "\n"
        "[tool.poetry.group.dev.dependencies]\n"
        'internal-dev-tool = { version = "^1.0", source = "internal" }\n'
    )

    _blanket, explicit_names = poetry_private_registry_context([pyproject])

    assert explicit_names == {"internal-dev-tool"}


def test_poetry_multiple_constraints_dependency_scoped_to_explicit_source(tmp_path: Path):
    """Poetry's documented "multiple constraints" list-form dependency
    (https://python-poetry.org/docs/dependency-specification/#multiple-
    constraints-dependencies) can mix a platform-scoped explicit-source
    variant with a plain-PyPI one, e.g. Poetry's own docs example:
    `foo = [{platform = "darwin", url = "..."}, {platform = "linux",
    version = "^1.0", source = "pypi"}]`. Confirmed live with a real
    `poetry lock` against an unreachable explicit source: the darwin-
    scoped variant's `source = "internal"` was genuinely consulted (the
    resolver tried to contact its URL, not PyPI) -- the same real
    resolution `test_poetry_dependency_pinned_to_explicit_source_is_scoped_private`
    already covers for a single-table spec. `spec.get("source")` isn't
    reachable on a list, so this shape was previously invisible here.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[[tool.poetry.source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple/"\n'
        'priority = "explicit"\n'
        "\n"
        "[tool.poetry.dependencies]\n"
        "platform-varying-pkg = [\n"
        '    { platform = "darwin", version = "^1.0", source = "internal" },\n'
        '    { platform = "linux", version = "^2.0", source = "pypi" }\n'
        "]\n"
    )

    blanket, explicit_names = poetry_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == {"platform-varying-pkg"}


def test_poetry_missing_file_is_not_an_error(tmp_path: Path):
    blanket, explicit_names = poetry_private_registry_context([tmp_path / "pyproject.toml"])

    assert blanket is False
    assert explicit_names == set()
