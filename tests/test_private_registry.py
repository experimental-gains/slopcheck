from pathlib import Path

from slopcheck import private_registry
from slopcheck.private_registry import (
    bunfig_private_registry_context,
    npm_private_registry_context,
    npm_scope,
    pdm_private_registry_context,
    pip_private_index_configured,
    pipfile_private_registry_context,
    poetry_private_registry_context,
    uv_private_registry_context,
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


def test_pip_private_index_from_requirements_txt_directive_bom_is_stripped(tmp_path: Path, monkeypatch):
    """Same BOM-tolerance guarantee as every other reader in this codebase
    (see parsers.py's utf-8-sig comment, and the `uv.toml`/pyproject.toml BOM
    tests below) — a requirements.txt some editors/tools write with a
    leading UTF-8 BOM is a real, valid file, not a hypothetical. Confirmed
    live with real pip (`pip install -r` against a BOM-prefixed requirements.txt
    whose first line was `-i http://127.0.0.1:9/simple`): pip's own output
    showed "Looking in indexes: http://127.0.0.1:9/simple" and real connection
    attempts to that address for a fake package name — the directive was
    genuinely honored despite the BOM. Before this fix, `path.read_text()`
    (no encoding) left the leading `﻿` glued onto the first line,
    `_PIP_DIRECTIVE_RE` (anchored at the line start) no longer matched, and
    this function silently returned False — misreporting a genuinely
    private-only dependency as `not_found` instead of `private`."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_bytes(b"\xef\xbb\xbf" + b"-i https://pypi.internal.example/simple\nrequests>=2.0\n")

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


def test_pip_private_index_from_requirements_txt_find_links(tmp_path: Path, monkeypatch):
    """`-f`/`--find-links` points pip at a flat file/local-directory source of
    archives, searched *in addition to* the configured index — a genuinely
    separate mechanism from `-i`/`--extra-index-url`, not a cosmetic
    alternative spelling of it. Confirmed live (pip 25.1.1): a real wheel
    built for a never-published name and dropped in a throwaway local
    directory, with a requirements.txt reading `-f /path/to/local-wheels`
    then an unqualified `totally-hallucinated-findlinks-xyz-123==1.0.0`,
    made `pip install --dry-run -r` genuinely find and "Would install" that
    exact version from the local directory ("Looking in links:
    /path/to/local-wheels"), while the public PyPI JSON API returned a plain
    404 for the same name the whole time. Before this fix, `_PIP_DIRECTIVE_RE`
    had no `-f`/`--find-links` branch at all, so slopcheck reported this
    genuinely-installable dependency as a plain `not_found` hallucination."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_FIND_LINKS", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("-f /path/to/local-wheels\ntotally-hallucinated-findlinks-xyz-123==1.0.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_requirements_txt_find_links_attached(tmp_path: Path, monkeypatch):
    """Same optparse short-option attachment pip accepts for `-i` (confirmed
    live, `-f/path/to/local-wheels` with no space resolved identically to the
    spaced form against a real local wheel directory)."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_FIND_LINKS", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("-f/path/to/local-wheels\ntotally-hallucinated-findlinks-xyz-123==1.0.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_requirements_txt_find_links_long_flag_equals(tmp_path: Path, monkeypatch):
    """pip also accepts the long-flag `=`-joined form (confirmed live,
    `--find-links=/path/to/local-wheels` resolved identically)."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_FIND_LINKS", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("--find-links=/path/to/local-wheels\ntotally-hallucinated-findlinks-xyz-123==1.0.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_find_links_env_var(tmp_path: Path, monkeypatch):
    """`PIP_FIND_LINKS` is pip's own env var equivalent of a requirements-file
    `-f`/`--find-links` line — confirmed live: with no `-f` anywhere in any
    scanned file, setting only `PIP_FIND_LINKS=/path/to/local-wheels` in the
    environment still made a real `pip install --dry-run -r` resolve a name
    that 404s on public PyPI straight from that directory."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.setenv("PIP_FIND_LINKS", "/path/to/local-wheels")
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("totally-hallucinated-findlinks-xyz-123==1.0.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_private_index_from_pip_conf_find_links(tmp_path: Path, monkeypatch):
    """`[global] find-links = ...` in pip.conf — confirmed live the same way
    as the requirements.txt directive above: a pip.conf with only this one
    setting (no `index-url`/`extra-index-url` at all) made a real
    `pip install --dry-run -r` resolve a name that 404s on public PyPI
    straight from the configured local directory."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_FIND_LINKS", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    pip_conf = tmp_path / "pip.conf"
    pip_conf.write_text("[global]\nfind-links = /path/to/local-wheels\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_conf))
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("totally-hallucinated-findlinks-xyz-123==1.0.0\n")

    assert pip_private_index_configured([reqs]) is True


def test_pip_config_has_extra_index_checks_underscore_find_links(tmp_path: Path, monkeypatch):
    """Same underscore-spelling normalization pip's own config loader applies
    to every option it reads (confirmed live, see
    test_pip_config_has_extra_index_checks_underscore_option_spelling above),
    just for `find_links` instead of `extra_index_url`."""
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nfind_links = /path/to/local-wheels\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(conf))

    assert private_registry._pip_config_has_extra_index() is True


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


def test_pip_private_index_from_sys_prefix_pip_conf_without_virtual_env(tmp_path: Path, monkeypatch):
    """pip's "site" config file is `os.path.join(sys.prefix, "pip.conf")`
    (pip._internal.configuration.get_configuration_files), read
    unconditionally by real pip regardless of the `VIRTUAL_ENV` environment
    variable — that var is only set when a venv was *activated* via its
    `activate` script. Invoking a venv's pip directly by path
    (`./venv/bin/pip install ...`, the standard Dockerfile/CI/Makefile
    pattern that never sources activate) leaves `VIRTUAL_ENV` unset while
    `sys.prefix` inside that process is still the venv root.

    Confirmed live: a real venv's own pip.conf (at the venv root) was
    genuinely honored by that venv's own pip with `VIRTUAL_ENV` explicitly
    unset (`env -u VIRTUAL_ENV /path/to/venv/bin/pip install --dry-run -v
    ...` showed "Looking in indexes: ..., http://127.0.0.1:9/simple"), and a
    slopcheck console-script installed into that same venv and invoked the
    same way reported a hallucinated dependency as `not_found` instead of
    `private` before this fix — `_pip_config_paths` only checked
    `$VIRTUAL_ENV/pip.conf`, never `sys.prefix`.
    """
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    pip_conf = venv_dir / "pip.conf"
    pip_conf.write_text("[install]\nextra-index-url = https://pypi.internal.example/simple\n")
    # Simulate slopcheck's own interpreter running from inside that venv
    # (sys.prefix is the venv root, sys.base_prefix is the system Python it
    # was created from) without anyone having sourced `activate`.
    monkeypatch.setattr(private_registry.sys, "prefix", str(venv_dir))
    monkeypatch.setattr(private_registry.sys, "base_prefix", str(tmp_path / "system-python"))

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


def test_npm_scope_registry_from_npmrc_bom_is_stripped(tmp_path: Path, monkeypatch):
    """Same BOM-tolerance guarantee as `pip_private_index_configured` above —
    confirmed live with real npm (`npm install --loglevel=verbose` against a
    BOM-prefixed `.npmrc` whose only content was
    `@acmecorp:registry=http://127.0.0.1:9/`, with a `@acmecorp/`-scoped fake
    dependency in package.json): npm's own verbose log showed a real fetch
    attempt to `http://127.0.0.1:9/...` for that scoped name, not the public
    registry — the scope mapping was genuinely honored despite the BOM.
    Before this fix, `rc_path.read_text()` (no encoding) left the BOM glued
    onto the first line, `_NPMRC_SCOPE_RE` no longer matched it, and this
    scope was silently missed — misreporting a genuinely private-only
    dependency as `not_found` instead of `private`."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".npmrc").write_bytes(
        b"\xef\xbb\xbf" + b"@acmecorp:registry=https://npm.internal.example/\n"
    )

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
    # Isolate from whatever venv is actually running this test suite —
    # sys.prefix != sys.base_prefix would otherwise add a real,
    # environment-dependent extra entry to the list this test asserts exactly.
    monkeypatch.setattr(private_registry.sys, "prefix", private_registry.sys.base_prefix)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._pip_config_paths() == [
        tmp_path / ".config" / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        Path("/etc/xdg/pip/pip.conf"),
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
    # Isolate from whatever venv is actually running this test suite —
    # sys.prefix != sys.base_prefix would otherwise add a real,
    # environment-dependent extra entry to the list this test asserts exactly.
    monkeypatch.setattr(private_registry.sys, "prefix", private_registry.sys.base_prefix)
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg = tmp_path / "customxdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert private_registry._pip_config_paths() == [
        xdg / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        Path("/etc/xdg/pip/pip.conf"),
        Path("/etc/pip.conf"),
    ]


def test_pip_config_paths_ignores_blank_xdg_config_home(tmp_path: Path, monkeypatch):
    """Real pip's platformdirs check is `if not path.strip()`, so an XDG_CONFIG_HOME
    set to the empty string (a real shell footgun: `export XDG_CONFIG_HOME=`) falls
    back to `~/.config`, the same as when it's unset entirely."""
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Isolate from whatever venv is actually running this test suite —
    # sys.prefix != sys.base_prefix would otherwise add a real,
    # environment-dependent extra entry to the list this test asserts exactly.
    monkeypatch.setattr(private_registry.sys, "prefix", private_registry.sys.base_prefix)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    assert private_registry._pip_config_paths()[0] == tmp_path / ".config" / "pip" / "pip.conf"


def test_pip_config_paths_uses_xdg_config_dirs_when_set(tmp_path: Path, monkeypatch):
    """pip's *system-wide* ("global") config lookup moves the same way its
    per-user one does, but via the sibling `XDG_CONFIG_DIRS` variable instead
    of `XDG_CONFIG_HOME` — confirmed live two ways against real pip
    (`pip install --dry-run -v` against an unreachable `127.0.0.1:9` index):
    with no environment customization at all, a `/etc/xdg/pip/pip.conf` on
    this box's real, unmodified filesystem was genuinely honored ("Looking in
    indexes: https://pypi.org/simple, http://127.0.0.1:9/simple"); with
    `XDG_CONFIG_DIRS` set to a custom path, the same pip.conf placed at
    `$XDG_CONFIG_DIRS/pip/pip.conf` was read *instead of*
    `/etc/xdg/pip/pip.conf` (platformdirs' own `_site_config_dirs` replaces,
    not adds to, the XDG default), matching pip's vendored
    platformdirs.unix.Unix._site_config_dirs implementation exactly."""
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Isolate from whatever venv is actually running this test suite —
    # sys.prefix != sys.base_prefix would otherwise add a real,
    # environment-dependent extra entry to the list this test asserts exactly.
    monkeypatch.setattr(private_registry.sys, "prefix", private_registry.sys.base_prefix)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg_dirs = tmp_path / "customxdgdirs"
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(xdg_dirs))

    assert private_registry._pip_config_paths() == [
        tmp_path / ".config" / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        xdg_dirs / "pip" / "pip.conf",
        Path("/etc/pip.conf"),
    ]


def test_pip_config_paths_supports_multiple_colon_separated_xdg_config_dirs(tmp_path: Path, monkeypatch):
    """`XDG_CONFIG_DIRS` is documented (and real pip/platformdirs actually
    implement it) as a colon-separated *list* of directories, not a single
    path — every one of them gets its own `pip.conf` check."""
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Isolate from whatever venv is actually running this test suite —
    # sys.prefix != sys.base_prefix would otherwise add a real,
    # environment-dependent extra entry to the list this test asserts exactly.
    monkeypatch.setattr(private_registry.sys, "prefix", private_registry.sys.base_prefix)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("XDG_CONFIG_DIRS", f"{first}:{second}")

    assert private_registry._pip_config_paths() == [
        tmp_path / ".config" / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        first / "pip" / "pip.conf",
        second / "pip" / "pip.conf",
        Path("/etc/pip.conf"),
    ]


def test_pip_config_paths_ignores_blank_xdg_config_dirs(tmp_path: Path, monkeypatch):
    """Same blank-value footgun rule as `XDG_CONFIG_HOME`: real platformdirs
    checks `if not path.strip()`, so `export XDG_CONFIG_DIRS=` falls back to
    the default `/etc/xdg`, the same as when it's unset entirely."""
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Isolate from whatever venv is actually running this test suite —
    # sys.prefix != sys.base_prefix would otherwise add a real,
    # environment-dependent extra entry to the list this test asserts exactly.
    monkeypatch.setattr(private_registry.sys, "prefix", private_registry.sys.base_prefix)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_DIRS", "")

    assert private_registry._pip_config_paths() == [
        tmp_path / ".config" / "pip" / "pip.conf",
        tmp_path / ".pip" / "pip.conf",
        Path("/etc/xdg/pip/pip.conf"),
        Path("/etc/pip.conf"),
    ]


def test_pip_private_index_from_xdg_config_dirs_pip_conf(tmp_path: Path, monkeypatch):
    """End-to-end regression for the XDG_CONFIG_DIRS gap: a private index
    configured only via `$XDG_CONFIG_DIRS/pip/pip.conf` (pip's real,
    genuinely-default *system-wide* config location — confirmed live, see
    `_pip_site_config_dirs`'s doc comment) must be detected, since a real
    `pip install` in that exact environment resolves it fine. Before this
    fix, `_pip_config_paths` never looked here at all, so this always
    returned False, and the caller (cli._downgrade_if_private) would report
    a legitimately-installable private-only dependency as a plain
    `not_found` hallucination instead of `private`/unverified."""
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_EXTRA_INDEX_URL", raising=False)
    monkeypatch.delenv("PIP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg_dirs = tmp_path / "customxdgdirs"
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(xdg_dirs))
    conf_dir = xdg_dirs / "pip"
    conf_dir.mkdir(parents=True)
    (conf_dir / "pip.conf").write_text("[global]\nextra-index-url = https://pypi.internal.example/simple\n")

    assert pip_private_index_configured([]) is True


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


def test_pip_config_has_extra_index_checks_underscore_option_spelling(tmp_path: Path, monkeypatch):
    # pip.conf's option keys are normalized by real pip's own config loader
    # (pip._internal.configuration._normalized_keys: `name.lower().replace(
    # "_", "-")`) before being read, so `extra_index_url = ...` (underscore)
    # is exactly as effective as the canonical `extra-index-url = ...`
    # (dash) spelling — confirmed live: `PIP_CONFIG_FILE=... pip config -v
    # list` against an underscore-only pip.conf reported
    # `global.extra-index-url=...`, and `pip install --dry-run -v` against
    # the same file with an unreachable `127.0.0.1:9` index genuinely
    # printed "Looking in indexes: https://pypi.org/simple,
    # http://127.0.0.1:9/simple". configparser itself does no such
    # underscore-to-dash normalization (only lowercasing), so checking only
    # the two dash-spelled option names via `parser.has_option` missed this
    # real, pip-equivalent spelling entirely.
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nextra_index_url = https://pypi.internal.example/simple\n")
    monkeypatch.setattr(private_registry, "_pip_config_paths", lambda: [conf])

    assert private_registry._pip_config_has_extra_index() is True


def test_pip_config_has_extra_index_checks_underscore_bare_index_url(tmp_path: Path, monkeypatch):
    conf = tmp_path / "pip.conf"
    conf.write_text("[global]\nindex_url = https://pypi.internal.example/simple\n")
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


def test_npm_private_registry_from_workspace_root_npmrc(tmp_path: Path, monkeypatch):
    """A scan rooted at a single npm workspace *member* directory (e.g. a CI
    job or pre-commit hook scoped to one changed package) must still see the
    enclosing workspace root's `.npmrc` — confirmed live (npm 9.2.0): a
    two-level layout (`root/package.json` with `"workspaces":
    ["packages/*"]` plus `root/.npmrc` carrying a scope mapping, and
    `root/packages/foo/package.json` with no `.npmrc` of its own) run from
    inside `packages/foo` genuinely loaded the root's `.npmrc` as its
    "project" config (debug log: "found workspace root at .../root" then
    "config:load:project" reading that directory's `.npmrc`). Before this
    fix, `_npmrc_paths`/`npm_private_registry_context` only ever looked at
    the exact directory holding the *scanned* package.json, so this scope
    mapping was invisible whenever the scan target was the member directory
    alone, misreporting a genuinely private-only dependency as plain
    `not_found` instead of downgraded to `private`."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    root = tmp_path / "root"
    member = root / "packages" / "foo"
    member.mkdir(parents=True)
    (root / "package.json").write_text('{"name": "root", "workspaces": ["packages/*"]}')
    (root / ".npmrc").write_text("@acmecorp:registry=https://npm.internal.example/\n")
    (member / "package.json").write_text('{"name": "foo", "version": "1.0.0"}')

    blanket, scopes = npm_private_registry_context([member])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_npm_private_registry_ignores_ancestor_npmrc_without_workspaces_match(
    tmp_path: Path, monkeypatch
):
    """The mirror image of the workspace-root fix above: confirmed live that
    a plain nested package.json with no enclosing `workspaces` field does
    *not* get this treatment — real npm then treats the nested package.json
    as its own project root and never reads the parent directory's `.npmrc`
    at all. Applying the workspace-root walk unconditionally (regardless of
    an actual `workspaces` pattern match) would risk the opposite bug: wrongly
    suppressing a genuine hallucination just because an unrelated ancestor
    directory happens to carry an `.npmrc`."""
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    root = tmp_path / "root"
    member = root / "sub"
    member.mkdir(parents=True)
    (root / "package.json").write_text('{"name": "root"}')
    (root / ".npmrc").write_text("@acmecorp:registry=https://npm.internal.example/\n")
    (member / "package.json").write_text('{"name": "sub", "version": "1.0.0"}')

    blanket, scopes = npm_private_registry_context([member])

    assert blanket is False
    assert scopes == set()


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
        "YARN_REGISTRY",
        "BUN_CONFIG_REGISTRY",
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


def test_yarnrc_blanket_registry_bom_is_stripped(tmp_path: Path, monkeypatch):
    """Same BOM-tolerance guarantee as `pip_private_index_configured` and the
    `.npmrc` scope test above — confirmed live with real Yarn Berry (4.18.1,
    via corepack) against a BOM-prefixed `.yarnrc.yml` whose only content was
    `npmRegistryServer: "http://127.0.0.1:9"`: resolving a fake dependency
    failed with "Unsafe http requests must be explicitly whitelisted ...
    (127.0.0.1)" — proof Yarn genuinely read that address out of the BOM'd
    file (the default registry is `registry.yarnpkg.com` over https, so this
    error only happens once the private-registry line was actually parsed).
    Before this fix, `rc_path.read_text()` (no encoding) left the BOM glued
    onto the first line, `_parse_yarnrc_registries`' line walker never
    matched the top-level `npmRegistryServer:` key, and this blanket
    override was silently missed."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc.yml").write_bytes(
        b"\xef\xbb\xbf" + b'npmRegistryServer: "https://npm.internal.example/"\n'
    )

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


def test_yarn_classic_rc_blanket_registry(tmp_path: Path, monkeypatch):
    """Yarn Classic (v1) has a *third* independent registry config file of
    its own, `.yarnrc` — plain `key "value"` lines, not the YAML
    `.yarnrc.yml` Yarn Berry reads, and entirely separate from `.npmrc` too.
    Confirmed live (Yarn Classic 1.22.22, `yarn install --verbose` against a
    scratch project with *only* a `.yarnrc` containing
    `registry "http://127.0.0.1:9/"` — no `.npmrc`, no `.yarnrc.yml`
    anywhere): the resolver genuinely performed a GET against
    `http://127.0.0.1:9/<name>` and failed with ECONNREFUSED, never
    contacting `registry.yarnpkg.com` at all. Before this fix, `.yarnrc`
    (Yarn Classic's own format) had no reader at all, so a Yarn-Classic
    project routing every dependency through a private registry this way
    had every genuinely-resolvable private dependency reported as a plain
    `not_found` hallucination."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc").write_text('registry "https://npm.internal.example/"\n')

    blanket, _scopes = npm_private_registry_context([tmp_path])

    assert blanket is True


def test_yarn_classic_rc_scope_registry(tmp_path: Path, monkeypatch):
    """Same gap, scoped form: confirmed live (Yarn Classic 1.22.22) that
    `"@acmecorp:registry" "http://127.0.0.1:9/"` in `.yarnrc` genuinely
    routed resolution of an `@acmecorp/`-scoped dependency at that address
    (a real GET to `http://127.0.0.1:9/@acmecorp%2f<name>`, ECONNREFUSED,
    not a 404 from the public registry) — an unscoped dependency in the same
    project is unaffected, same as every other scope mapping this file
    already handles."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc").write_text('"@acmecorp:registry" "https://npm.internal.example/"\n')

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_yarn_classic_rc_from_workspace_root(tmp_path: Path, monkeypatch):
    """Same workspace-root gap as `.npmrc` (see
    `test_npm_private_registry_from_workspace_root_npmrc`), for Yarn
    Classic's own `.yarnrc` — confirmed live (Yarn Classic 1.22.22):
    `yarn config get` run from inside a workspace member directory with no
    `.yarnrc` of its own genuinely walked up to the enclosing workspace
    root and used the scope mapping found only there (verbose log: checked
    every ancestor level's `.yarnrc`, found and used the one at the
    workspace root)."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    root = tmp_path / "root"
    member = root / "packages" / "foo"
    member.mkdir(parents=True)
    (root / "package.json").write_text('{"name": "root", "workspaces": ["packages/*"]}')
    (root / ".yarnrc").write_text('"@acmecorp:registry" "https://npm.internal.example/"\n')
    (member / "package.json").write_text('{"name": "foo", "version": "1.0.0"}')

    blanket, scopes = npm_private_registry_context([member])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_yarn_classic_rc_unquoted_value_not_honored(tmp_path: Path, monkeypatch):
    """The mirror image of the above: confirmed live that real Yarn Classic
    itself silently ignores an unquoted `.yarnrc` value (it fell straight
    through to the public registry, no connection attempt at the named
    address at all) — so this parser must not treat that shape as a
    private-registry signal either, or it would wrongly downgrade a real
    hallucination in an otherwise-ordinary project to `private` and swallow
    it."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / ".yarnrc").write_text("registry https://npm.internal.example/\n")

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == set()


def test_yarn_classic_registry_env_var_triggers_blanket(tmp_path: Path, monkeypatch):
    """`YARN_REGISTRY` is Yarn Classic's own env var equivalent of the
    blanket `.yarnrc` form — confirmed live (real `yarn install`, no
    `.yarnrc` at all) that it's genuinely honored, case-insensitively the
    same way `npm_config_registry`/`YARN_NPM_REGISTRY_SERVER` already are
    (a mixed-case `Yarn_Registry` spelling was confirmed live too)."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("Yarn_Registry", "https://npm.internal.example/")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, _scopes = npm_private_registry_context([])

    assert blanket is True


def test_yarn_classic_rc_home_global_config_is_read(tmp_path: Path, monkeypatch):
    """Yarn Classic merges a home-directory `~/.yarnrc` in as a global
    config too, the same project+global split every other config reader in
    this file already has — confirmed live: a blanket `registry "..."`
    placed only there (no project-level `.yarnrc` at all) was genuinely
    honored by a real `yarn install`. This is the non-root case
    specifically (real Yarn only uses `$HOME` when not running as root —
    see `test_yarn_classic_config_home_relocates_for_root` below), so uid 0
    is pinned to a non-root value to make this test hermetic regardless of
    the actual user running the test suite."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".yarnrc").write_text('registry "https://npm.internal.example/"\n')
    project_root = tmp_path / "project"
    project_root.mkdir()

    blanket, _scopes = npm_private_registry_context([project_root])

    assert blanket is True


def test_yarn_classic_rc_missing_file_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_registry_env(monkeypatch)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, scopes = npm_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == set()


def test_yarn_classic_config_home_uses_home_when_not_root(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FAKEROOTKEY", raising=False)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._yarn_classic_rc_paths([]) == [tmp_path / ".yarnrc"]


def test_yarn_classic_config_home_relocates_for_root(tmp_path: Path, monkeypatch):
    """Real Yarn Classic (v1) deliberately avoids writing its *global*
    config under root's actual home directory — confirmed by reading the
    real, installed 1.22.22 npm package's bundled `cli.js` directly: its
    `root-user`/`user-home` helper modules resolve the config-home
    directory as `isRootUser(process.getuid()) && !isFakeRoot() ?
    path.resolve('/usr/local/share') : os.homedir()`. Confirmed live too:
    running actual Yarn Classic 1.22.22 as root (`process.getuid() === 0`,
    no `FAKEROOTKEY`), a blanket `registry "..."` placed *only* in
    `/usr/local/share/.yarnrc` — no project `.yarnrc`, no `$HOME/.yarnrc`
    at all — was genuinely honored by a real `yarn install --verbose` (a
    GET to the configured address, ECONNREFUSED, never touching
    `registry.yarnpkg.com`). Root is an extremely common way to run both
    real installs (Docker/CI base images) and slopcheck itself, so
    `Path.home()` alone silently missed this before this fix."""
    monkeypatch.delenv("FAKEROOTKEY", raising=False)
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._yarn_classic_rc_paths([]) == [Path("/usr/local/share") / ".yarnrc"]


def test_yarn_classic_config_home_fakeroot_keeps_real_home(tmp_path: Path, monkeypatch):
    """`fakeroot(1)` is real Yarn's own documented escape hatch from the
    root relocation above (`isFakeRoot()` in the same source, gated on a
    `FAKEROOTKEY` env var `fakeroot` itself sets) — a `fakeroot`-wrapped
    process still reports uid 0 via `process.getuid()` but real Yarn
    deliberately treats it as an ordinary, non-relocated user."""
    monkeypatch.setattr(private_registry.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAKEROOTKEY", "1234")

    assert private_registry._yarn_classic_rc_paths([]) == [tmp_path / ".yarnrc"]


def test_bunfig_scope_registry(tmp_path: Path, monkeypatch):
    """Bun (https://bun.sh/docs/install/registries) reads its own
    `bunfig.toml`, entirely independent of `.npmrc` — confirmed live (Bun
    1.4.2): a project with only a `bunfig.toml` and a scoped
    `[install.scopes]` entry genuinely routed resolution of that scope at
    the configured address (`ConnectionRefused` to a closed local port,
    not a 404 from the public registry). Unlike Yarn's `npmScopes` keys,
    Bun's own `[install.scopes]` keys are already `@`-prefixed."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / "bunfig.toml").write_text(
        '[install.scopes]\n"@acmecorp" = "https://npm.internal.example/"\n'
    )

    blanket, scopes = bunfig_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_bunfig_scope_registry_table_form(tmp_path: Path, monkeypatch):
    """A scope value can also be a `{ url = ..., token = ... }` table
    (Bun's authenticated-registry form) rather than a plain string —
    confirmed live, same `ConnectionRefused` tell as the plain-string
    form."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / "bunfig.toml").write_text(
        '[install.scopes]\n"@acmecorp" = { url = "https://npm.internal.example/", token = "abc" }\n'
    )

    blanket, scopes = bunfig_private_registry_context([tmp_path])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_bunfig_blanket_registry(tmp_path: Path, monkeypatch):
    """`[install].registry` with no `[install.scopes]` at all routes every
    package through it — confirmed live, the Bun analog of npm's
    `registry=` line."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / "bunfig.toml").write_text('[install]\nregistry = "https://npm.internal.example/"\n')

    blanket, _scopes = bunfig_private_registry_context([tmp_path])

    assert blanket is True


def test_bunfig_blanket_registry_table_form(tmp_path: Path, monkeypatch):
    """`[install].registry` can also be a `{ url = ..., token = ... }`
    table rather than a plain string — confirmed live, same
    `ConnectionRefused` tell as the plain-string form."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    (tmp_path / "bunfig.toml").write_text(
        '[install]\nregistry = { url = "https://npm.internal.example/", token = "abc" }\n'
    )

    blanket, _scopes = bunfig_private_registry_context([tmp_path])

    assert blanket is True


def test_bunfig_env_var_triggers_blanket(tmp_path: Path, monkeypatch):
    """`BUN_CONFIG_REGISTRY` is the env var equivalent of the blanket
    `bunfig.toml` form — confirmed live, mirrors npm's
    `npm_config_registry`."""
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("BUN_CONFIG_REGISTRY", "https://npm.internal.example/")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, _scopes = bunfig_private_registry_context([])

    assert blanket is True


def test_bunfig_home_global_config_is_read(tmp_path: Path, monkeypatch):
    """A user-level `~/.bunfig.toml` is read too — confirmed live, same
    `ConnectionRefused` tell, with no project-root `bunfig.toml` present
    at all — the Bun analogue of npm's userconfig / Yarn's home-directory
    `.yarnrc.yml` fallback."""
    _clear_registry_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".bunfig.toml").write_text(
        '[install.scopes]\n"@acmecorp" = "https://npm.internal.example/"\n'
    )

    blanket, scopes = bunfig_private_registry_context([tmp_path / "project"])

    assert blanket is False
    assert scopes == {"@acmecorp"}


def test_bunfig_missing_file_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_registry_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()

    blanket, scopes = bunfig_private_registry_context([tmp_path])

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


def test_pipfile_no_source_table_is_not_private(tmp_path: Path):
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text('[packages]\nrequests = "*"\n')

    blanket, explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is False
    assert explicit_names == set()


def test_pipfile_default_public_pypi_source_is_not_private(tmp_path: Path):
    """The exact boilerplate `pipenv` itself writes into every generated
    Pipfile -- confirming this ordinary, extremely common case doesn't get
    swept up as blanket-private just because a `[[source]]` table exists at
    all (unlike Poetry's optional source table, Pipenv's is mandatory)."""
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "verify_ssl = true\n"
        "\n"
        "[packages]\n"
        'requests = "*"\n'
    )

    blanket, explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is False
    assert explicit_names == set()


def test_pipfile_first_source_replaced_is_blanket(tmp_path: Path):
    """Confirmed live (`pipenv lock` under Pipenv 2026.8.0): with the
    conventionally-named "pypi" source's own `url` replaced by a private
    mirror, a plain undecorated dependency resolves only against it --
    `127.0.0.1:9` genuinely contacted, real pypi.org never touched."""
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://127.0.0.1:9/simple"\n'
        "verify_ssl = true\n"
        "\n"
        "[packages]\n"
        'internal-only-pkg = "*"\n'
    )

    blanket, _explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is True


def test_pipfile_default_is_the_first_listed_source_not_the_one_named_pypi(tmp_path: Path):
    """Confirmed live: Pipenv's default index for an undecorated dependency
    is whichever `[[source]]` table comes first in file order -- not the
    entry named "pypi" specifically. A later, differently-ordered public
    "pypi" entry does not make this file non-private."""
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "internal"\n'
        'url = "https://127.0.0.1:9/simple"\n'
        "\n"
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "\n"
        "[packages]\n"
        'internal-only-pkg = "*"\n'
    )

    blanket, _explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is True


def test_pipfile_dependency_pinned_to_named_index_is_scoped_private(tmp_path: Path):
    """Confirmed live: a dependency's own `index = "<name>"` key routes it
    to exactly that source -- the public "pypi" source is never contacted
    for that name -- while an undecorated sibling dependency still resolves
    against the public default and stays out of `explicit_names`."""
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "\n"
        "[[source]]\n"
        'name = "internal"\n'
        'url = "https://127.0.0.1:9/simple"\n'
        "\n"
        "[packages]\n"
        'internal-only-pkg = { version = "*", index = "internal" }\n'
        'public-pkg = "*"\n'
    )

    blanket, explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is False
    assert explicit_names == {"internal-only-pkg"}


def test_pipfile_dev_packages_also_scoped(tmp_path: Path):
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "\n"
        "[[source]]\n"
        'name = "internal"\n'
        'url = "https://127.0.0.1:9/simple"\n'
        "\n"
        "[dev-packages]\n"
        'internal-dev-tool = { version = "*", index = "internal" }\n'
    )

    _blanket, explicit_names = pipfile_private_registry_context([pipfile])

    assert explicit_names == {"internal-dev-tool"}


def test_pipfile_index_key_naming_the_public_source_is_not_scoped_private(tmp_path: Path):
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "\n"
        "[packages]\n"
        'ordinary-pkg = { version = "*", index = "pypi" }\n'
    )

    blanket, explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is False
    assert explicit_names == set()


def test_pipfile_missing_file_is_not_an_error(tmp_path: Path):
    blanket, explicit_names = pipfile_private_registry_context([tmp_path / "Pipfile"])

    assert blanket is False
    assert explicit_names == set()


def _clear_pipenv_env(monkeypatch) -> None:
    monkeypatch.delenv("PIPENV_PYPI_MIRROR", raising=False)


def test_pipfile_pypi_mirror_env_var_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live (`pipenv lock` under Pipenv 2026.8.0): with the
    conventional `name = "pypi", url = "https://pypi.org/simple"` as the
    Pipfile's only source, setting `PIPENV_PYPI_MIRROR` alone (no Pipfile
    change at all) made a real `pipenv lock` genuinely attempt a connection
    to the mirror's address for an undecorated dependency instead of
    pypi.org."""
    _clear_pipenv_env(monkeypatch)
    monkeypatch.setenv("PIPENV_PYPI_MIRROR", "http://127.0.0.1:9/simple")
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "verify_ssl = true\n"
        "\n"
        "[packages]\n"
        'internal-only-pkg = "*"\n'
    )

    blanket, _explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is True


def test_pipfile_pypi_mirror_env_var_with_no_source_table_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live: Pipenv's own implicit default source (used when a
    Pipfile has no `[[source]]` table at all) is public PyPI, and
    `PIPENV_PYPI_MIRROR` replaces it the same way it replaces an explicit
    `name = "pypi"` source -- a real `pipenv lock` genuinely attempted the
    mirror's address with zero `[[source]]` anywhere in the Pipfile."""
    _clear_pipenv_env(monkeypatch)
    monkeypatch.setenv("PIPENV_PYPI_MIRROR", "http://127.0.0.1:9/simple")
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text('[packages]\ninternal-only-pkg = "*"\n')

    blanket, _explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is True


def test_pipfile_pypi_mirror_env_var_set_to_public_pypi_is_not_blanket(tmp_path: Path, monkeypatch):
    _clear_pipenv_env(monkeypatch)
    monkeypatch.setenv("PIPENV_PYPI_MIRROR", "https://pypi.org/simple")
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "pypi"\n'
        'url = "https://pypi.org/simple"\n'
        "\n"
        "[packages]\n"
        'requests = "*"\n'
    )

    blanket, explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is False
    assert explicit_names == set()


def test_pipfile_pypi_mirror_env_var_does_not_affect_already_private_default(tmp_path: Path, monkeypatch):
    """The mirror env var only overwrites a source whose *current* url is
    public PyPI (`is_pypi_url`) -- a Pipfile whose first source is already a
    private mirror is unaffected, and this function's existing
    non-public-URL check already covers it regardless of the env var."""
    _clear_pipenv_env(monkeypatch)
    monkeypatch.setenv("PIPENV_PYPI_MIRROR", "http://127.0.0.1:9/simple")
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        "[[source]]\n"
        'name = "internal"\n'
        'url = "https://pkgs.internal.example/simple"\n'
        "\n"
        "[packages]\n"
        'internal-only-pkg = "*"\n'
    )

    blanket, _explicit_names = pipfile_private_registry_context([pipfile])

    assert blanket is True


def _clear_uv_env(monkeypatch) -> None:
    for var in (*private_registry._UV_ENV_BLANKET_VARS, "UV_CONFIG_FILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_uv_no_config_is_not_private(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_non_explicit_index_in_pyproject_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live (real `uv lock` against an unreachable
    `127.0.0.1:9/simple` index): a `[[tool.uv.index]]` entry with no
    `explicit = true` is consulted for *every* dependency, not just ones
    that name it via `[tool.uv.sources]` — the uv equivalent of pip's
    `--extra-index-url` and Poetry's non-explicit source."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True
    assert explicit_names == set()


def test_uv_explicit_index_is_scoped_not_blanket(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg", "requests"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = { index = "internal" }\n'
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == {"totally-fake-pkg"}


def test_uv_explicit_index_name_match_is_normalized(tmp_path: Path, monkeypatch):
    """`[tool.uv.sources]` keys follow the same PEP 503 name equivalence as
    every other pyproject.toml dependency table — confirmed against uv's own
    real behavior elsewhere in this codebase (`_normalize_name`'s docstring
    in parsers.py)."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["Totally_Fake.Pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        '"totally-fake-pkg" = { index = "internal" }\n'
    )

    _blanket, explicit_names = uv_private_registry_context([pyproject])

    assert explicit_names == {"totally-fake-pkg"}


def test_uv_explicit_index_unreferenced_by_any_source_is_not_scoped(tmp_path: Path, monkeypatch):
    """Confirmed live: a real `uv lock` never even connects to an explicit
    index's URL for a dependency that doesn't opt into it via `[tool.uv.
    sources]` — mirrors Poetry's `priority = "explicit"` behavior."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["requests"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_env_var_is_blanket(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://pypi.internal.example/simple")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')

    blanket, _explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True


def test_uv_find_links_env_var_is_blanket(tmp_path: Path, monkeypatch):
    """`UV_FIND_LINKS` is uv's own env var equivalent of a `find-links`
    config key — confirmed live (uv 0.12.19, a hand-built wheel for a
    never-published name in a throwaway local directory, no `[[index]]`/
    `[[tool.uv.index]]` anywhere): setting only `UV_FIND_LINKS=<that
    directory>` made a real `uv lock` genuinely resolve the name straight
    from it, the same way `PIP_FIND_LINKS` already does for pip."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UV_FIND_LINKS", "/path/to/local-wheels")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n')

    blanket, _explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True


def test_uv_find_links_in_pyproject_is_blanket(tmp_path: Path, monkeypatch):
    """`[tool.uv] find-links = [...]` is uv's own analog of pip's
    `-f`/`--find-links` — a flat local-directory/HTML-page source searched
    *in addition to* any configured index, not scoped by `[tool.uv.sources]`
    the way an `explicit = true` index entry is. Confirmed live (uv 0.12.19):
    a project with this key and no index/source config at all still
    resolved a name absent from public PyPI straight from the named
    directory. Before this fix, `uv_private_registry_context` had no
    `find-links` handling at all — the same gap already fixed for pip's own
    `-f`/`--find-links` (see `_PIP_DIRECTIVE_RE`) — so this genuinely
    private-only dependency was reported as a plain `not_found`
    hallucination instead of downgraded to `private`."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[tool.uv]\n"
        'find-links = ["/path/to/local-wheels"]\n'
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True
    assert explicit_names == set()


def test_uv_find_links_in_standalone_uv_toml_is_blanket(tmp_path: Path, monkeypatch):
    """Same `find-links` gap as `test_uv_find_links_in_pyproject_is_blanket`,
    but set in a standalone `uv.toml` instead — confirmed live the same way,
    zero `[tool.uv]` section in pyproject.toml at all."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n')
    (tmp_path / "uv.toml").write_text('find-links = ["/path/to/local-wheels"]\n')

    blanket, _explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True


def test_uv_find_links_empty_list_is_not_blanket(tmp_path: Path, monkeypatch):
    """An empty `find-links = []` (the default shape, equivalent to the key
    being absent) shouldn't be treated as a private-registry signal."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["requests"]\n\n[tool.uv]\nfind-links = []\n'
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_config_paths_uses_xdg_config_home_when_set(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    xdg = tmp_path / "customxdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert private_registry._uv_config_paths([]) == [xdg / "uv" / "uv.toml"]


def test_uv_config_paths_ignores_blank_xdg_config_home(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    assert private_registry._uv_config_paths([]) == [tmp_path / ".config" / "uv" / "uv.toml"]


def test_uv_private_index_from_standalone_uv_toml(tmp_path: Path, monkeypatch):
    """The uv equivalent of the project-`Pipfile`/`pyproject.toml`-only
    signals above: uv's own private-index config can live entirely outside
    pyproject.toml, in a sibling `uv.toml` — confirmed live (real `uv lock`
    with zero `[tool.uv]` section in pyproject.toml at all)."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n')
    (tmp_path / "uv.toml").write_text(
        '[[index]]\nname = "internal"\nurl = "https://pypi.internal.example/simple"\n'
    )

    blanket, _explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True


def test_uv_config_file_env_var_overrides_search(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    override = tmp_path / "somewhere" / "uv.toml"
    override.parent.mkdir()
    monkeypatch.setenv("UV_CONFIG_FILE", str(override))

    assert private_registry._uv_config_paths([tmp_path]) == [override]


def test_uv_explicit_index_declared_only_in_uv_toml_still_scopes_pyproject_sources(
    tmp_path: Path, monkeypatch
):
    """The index table and the `[tool.uv.sources]` reference to it can live
    in different files — confirmed live (`uv.toml`'s `index` field wins over
    a sibling pyproject.toml's `[tool.uv.index]` when both exist, but
    `[tool.uv.sources]` is read from pyproject.toml regardless)."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = { index = "internal" }\n'
    )
    (tmp_path / "uv.toml").write_text(
        '[[index]]\nname = "internal"\nurl = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
    )

    assert uv_private_registry_context([pyproject]) == (False, {"totally-fake-pkg"})


def test_uv_workspace_member_scanned_alone_sees_root_blanket_index(tmp_path: Path, monkeypatch):
    """Regression test for a real-world find: confirmed live (uv 0.12.19) —
    a workspace root pyproject.toml's non-explicit `[[tool.uv.index]]` is
    genuinely consulted for a member's dependency even when `uv lock` is
    run from *inside* the member directory alone (uv discovers the
    workspace root automatically, the same single-shared-lockfile behavior
    as running from the root itself). Before this fix, scanning only the
    member's own pyproject.toml (a realistic shape: a monorepo CI job
    scoped to one changed package) never saw the root's index config at
    all, since the root pyproject.toml isn't even among the scanned paths.
    """
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "root"
    member = root / "pkgs" / "foo"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\ndependencies = []\n'
        "\n[tool.uv.workspace]\n"
        'members = ["pkgs/*"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
    )
    member_pyproject = member / "pyproject.toml"
    member_pyproject.write_text('[project]\nname = "foo"\ndependencies = ["totally-fake-pkg"]\n')

    blanket, explicit_names = uv_private_registry_context([member_pyproject])

    assert blanket is True
    assert explicit_names == set()


def test_uv_workspace_member_scanned_alone_sees_root_explicit_index_source(tmp_path: Path, monkeypatch):
    """The cross-file counterpart of `test_uv_workspace_root_...`: an
    `explicit = true` index declared only in the workspace root's
    pyproject.toml, referenced by a member's own `[tool.uv.sources]` --
    confirmed live (uv 0.12.19) that a real `uv lock` run from inside the
    member alone genuinely resolves this, going straight to the configured
    index for exactly that one dependency and nothing else."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "root"
    member = root / "pkgs" / "baz"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\ndependencies = []\n'
        "\n[tool.uv.workspace]\n"
        'members = ["pkgs/*"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
    )
    member_pyproject = member / "pyproject.toml"
    member_pyproject.write_text(
        '[project]\nname = "baz"\ndependencies = ["totally-fake-pkg", "requests"]\n'
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = { index = "internal" }\n'
    )

    blanket, explicit_names = uv_private_registry_context([member_pyproject])

    assert blanket is False
    assert explicit_names == {"totally-fake-pkg"}


def test_uv_workspace_excluded_member_scanned_alone_sees_no_root_config(tmp_path: Path, monkeypatch):
    """The mirror image: confirmed live (uv 0.12.19) that a directory listed
    in `exclude` is genuinely treated as outside the workspace -- its own
    `uv lock` only ever contacts public PyPI, never the root's configured
    private index. Matching this matters: wrongly inheriting the root's
    config for an excluded directory would downgrade a real hallucination
    there to `private` and silently swallow it."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "root"
    member = root / "pkgs" / "excluded"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\ndependencies = []\n'
        "\n[tool.uv.workspace]\n"
        'members = ["pkgs/*"]\n'
        'exclude = ["pkgs/excluded"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
    )
    member_pyproject = member / "pyproject.toml"
    member_pyproject.write_text('[project]\nname = "excluded"\ndependencies = ["totally-fake-pkg"]\n')

    blanket, explicit_names = uv_private_registry_context([member_pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_workspace_member_scanned_alone_sees_root_find_links(tmp_path: Path, monkeypatch):
    """`find-links` folds into `blanket` the same way a non-explicit index
    does, so the workspace-root inheritance fix needs to cover it too."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "root"
    member = root / "pkgs" / "foo"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\ndependencies = []\n'
        "\n[tool.uv.workspace]\n"
        'members = ["pkgs/*"]\n'
        "\n[tool.uv]\n"
        'find-links = ["/path/to/local-wheels"]\n'
    )
    member_pyproject = member / "pyproject.toml"
    member_pyproject.write_text('[project]\nname = "foo"\ndependencies = ["totally-fake-pkg"]\n')

    blanket, _explicit_names = uv_private_registry_context([member_pyproject])

    assert blanket is True


def test_uv_workspace_member_scanned_alone_sees_root_standalone_uv_toml(tmp_path: Path, monkeypatch):
    """The workspace-root inheritance also needs to reach a standalone
    `uv.toml` sitting at the root, not just `[tool.uv]` in the root's
    pyproject.toml."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "root"
    member = root / "pkgs" / "foo"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\ndependencies = []\n\n[tool.uv.workspace]\nmembers = ["pkgs/*"]\n'
    )
    (root / "uv.toml").write_text(
        '[[index]]\nname = "internal"\nurl = "https://pypi.internal.example/simple"\n'
    )
    member_pyproject = member / "pyproject.toml"
    member_pyproject.write_text('[project]\nname = "foo"\ndependencies = ["totally-fake-pkg"]\n')

    blanket, _explicit_names = uv_private_registry_context([member_pyproject])

    assert blanket is True


def test_uv_index_entries_ignores_non_list_value(tmp_path: Path, monkeypatch):
    """A malformed `index = "not-a-table-array"` (real uv itself hard-errors
    on this shape, "invalid type: string, expected a sequence", confirmed
    live) shouldn't crash the scan — same defensive shape as
    `poetry_private_registry_context`'s `isinstance(sources, list)` check."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["requests"]\n\n[tool.uv]\nindex = "oops"\n'
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_toml_malformed_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')
    (tmp_path / "uv.toml").write_text("this is not valid toml [[[")

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_missing_pyproject_file_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    blanket, explicit_names = uv_private_registry_context([tmp_path / "pyproject.toml"])

    assert blanket is False
    assert explicit_names == set()


def test_uv_pyproject_malformed_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("this is not valid toml [[[")

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_tool_uv_section_not_a_table_is_ignored(tmp_path: Path, monkeypatch):
    """A `[tool] uv = "oops"` (uv itself would ignore/error on this shape
    too) shouldn't crash the scan."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n\n[tool]\nuv = "oops"\n')

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_sources_not_a_table_is_ignored(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv]\n"
        'sources = "oops"\n'
    )

    blanket, explicit_names = uv_private_registry_context([pyproject])

    assert blanket is False
    assert explicit_names == set()


def test_uv_config_paths_falls_back_to_home_when_xdg_unset(tmp_path: Path, monkeypatch):
    """Distinct from the blank-string case above: `XDG_CONFIG_HOME` simply
    absent from the environment must hit the same `~/.config` fallback as
    when it's present-but-blank."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("UV_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert private_registry._uv_config_paths([]) == [tmp_path / ".config" / "uv" / "uv.toml"]


def test_uv_toml_bom_is_stripped(tmp_path: Path, monkeypatch):
    """Same BOM-tolerance guarantee as every other TOML/JSON reader in this
    codebase (see parsers.py's utf-8-sig comment) — a real, valid file some
    editors/tools write."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')
    (tmp_path / "uv.toml").write_bytes(
        b"\xef\xbb\xbf" + b'[[index]]\nname = "internal"\nurl = "https://pypi.internal.example/simple"\n'
    )

    blanket, _explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True


def test_uv_pyproject_bom_is_stripped(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(
        b"\xef\xbb\xbf"
        + b'[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        + b"\n[[tool.uv.index]]\n"
        + b'name = "internal"\n'
        + b'url = "https://pypi.internal.example/simple"\n'
    )

    blanket, _explicit_names = uv_private_registry_context([pyproject])

    assert blanket is True


def test_uv_config_missing_file_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    """A missing `uv.toml` candidate for one project root must not stop the
    scan from reading a real one at another root — this scans multiple
    project roots in a single call the same way a monorepo would."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "pyproject.toml").write_text('[project]\nname = "a"\ndependencies = ["requests"]\n')
    (root_b / "pyproject.toml").write_text('[project]\nname = "b"\ndependencies = ["requests"]\n')
    # root_a gets no uv.toml at all; root_b's is the only real one.
    (root_b / "uv.toml").write_text('[[index]]\nname = "internal"\nurl = "https://pypi.internal.example/simple"\n')

    blanket, _explicit_names = uv_private_registry_context(
        [root_a / "pyproject.toml", root_b / "pyproject.toml"]
    )

    assert blanket is True


def test_uv_pyproject_missing_file_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    """Distinct from the uv.toml-missing case above: here the `pyproject.
    toml` path itself doesn't exist (e.g. a stale path from an earlier scan
    step) — the second loop over `pyproject_paths` must still read a real
    one later in the list rather than stopping."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    missing = tmp_path / "missing" / "pyproject.toml"
    good = tmp_path / "good" / "pyproject.toml"
    good.parent.mkdir()
    good.write_text(
        '[project]\nname = "good"\ndependencies = ["requests"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
    )

    blanket, _explicit_names = uv_private_registry_context([missing, good])

    assert blanket is True


def test_uv_config_malformed_file_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "pyproject.toml").write_text('[project]\nname = "a"\ndependencies = ["requests"]\n')
    (root_b / "pyproject.toml").write_text('[project]\nname = "b"\ndependencies = ["requests"]\n')
    (root_a / "uv.toml").write_text("this is not valid toml [[[")
    (root_b / "uv.toml").write_text('[[index]]\nname = "internal"\nurl = "https://pypi.internal.example/simple"\n')

    blanket, _explicit_names = uv_private_registry_context(
        [root_a / "pyproject.toml", root_b / "pyproject.toml"]
    )

    assert blanket is True


def test_uv_pyproject_malformed_file_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    broken = tmp_path / "broken" / "pyproject.toml"
    broken.parent.mkdir()
    broken.write_text("this is not valid toml [[[")
    good = tmp_path / "good" / "pyproject.toml"
    good.parent.mkdir()
    good.write_text(
        '[project]\nname = "good"\ndependencies = ["requests"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
    )

    blanket, _explicit_names = uv_private_registry_context([broken, good])

    assert blanket is True


def test_uv_pyproject_non_dict_uv_section_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    oops = tmp_path / "oops" / "pyproject.toml"
    oops.parent.mkdir()
    oops.write_text('[project]\nname = "oops"\ndependencies = ["requests"]\n\n[tool]\nuv = "oops"\n')
    good = tmp_path / "good" / "pyproject.toml"
    good.parent.mkdir()
    good.write_text(
        '[project]\nname = "good"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = { index = "internal" }\n'
    )

    _blanket, explicit_names = uv_private_registry_context([oops, good])

    assert explicit_names == {"totally-fake-pkg"}


def test_uv_no_explicit_index_yet_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    """The first pyproject.toml in the list declares no uv config at all
    (`explicit_index_names` is still empty when its own sources-check would
    run), so it takes the "skip this file's sources" branch — that must
    move on to the next file, not stop the whole scan."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    plain = tmp_path / "plain" / "pyproject.toml"
    plain.parent.mkdir()
    plain.write_text('[project]\nname = "plain"\ndependencies = ["requests"]\n')
    good = tmp_path / "good" / "pyproject.toml"
    good.parent.mkdir()
    good.write_text(
        '[project]\nname = "good"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = { index = "internal" }\n'
    )

    _blanket, explicit_names = uv_private_registry_context([plain, good])

    assert explicit_names == {"totally-fake-pkg"}


def test_uv_non_dict_sources_does_not_abort_remaining_paths(tmp_path: Path, monkeypatch):
    """The first pyproject.toml declares its own explicit index (so
    `explicit_index_names` is already non-empty by the time its `sources`
    field is checked) but sets `sources` to a malformed non-table value —
    that must skip just this file's sources, not the rest of the scan."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    malformed = tmp_path / "malformed" / "pyproject.toml"
    malformed.parent.mkdir()
    malformed.write_text(
        '[project]\nname = "malformed"\ndependencies = ["requests"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "other"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv]\n"
        'sources = "oops"\n'
    )
    good = tmp_path / "good" / "pyproject.toml"
    good.parent.mkdir()
    good.write_text(
        '[project]\nname = "good"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = { index = "internal" }\n'
    )

    _blanket, explicit_names = uv_private_registry_context([malformed, good])

    assert explicit_names == {"totally-fake-pkg"}


def test_uv_sources_list_form_scopes_when_any_entry_matches(tmp_path: Path, monkeypatch):
    """`[tool.uv.sources]` entries can be a list of per-platform/marker
    source tables (uv's documented multi-source syntax, already relied on
    by `_is_uv_registry_source` in parsers.py) rather than a single table —
    confirmed real by that existing code's own justification. Mirrors the
    Poetry multiple-constraints gap this codebase already found and fixed
    (see `poetry_private_registry_context`'s docstring)."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = [\n'
        '  { index = "internal", marker = "sys_platform == \'linux\'" },\n'
        '  { git = "https://example.com/other.git", marker = "sys_platform == \'darwin\'" },\n'
        "]\n"
    )

    _blanket, explicit_names = uv_private_registry_context([pyproject])

    assert explicit_names == {"totally-fake-pkg"}


def test_uv_sources_list_form_ignores_non_table_entries(tmp_path: Path, monkeypatch):
    """A list-form `[tool.uv.sources]` entry mixing a non-table item with a
    real one (malformed, but shouldn't crash the scan) — same defensive
    shape as `poetry_private_registry_context`'s `isinstance(source, dict)`
    check inside its own sources loop."""
    _clear_uv_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["totally-fake-pkg"]\n'
        "\n[[tool.uv.index]]\n"
        'name = "internal"\n'
        'url = "https://pypi.internal.example/simple"\n'
        "explicit = true\n"
        "\n[tool.uv.sources]\n"
        'totally-fake-pkg = [\n'
        '  "not-a-table",\n'
        '  { index = "internal" },\n'
        "]\n"
    )

    _blanket, explicit_names = uv_private_registry_context([pyproject])

    assert explicit_names == {"totally-fake-pkg"}


def test_pdm_no_source_table_is_not_private(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')

    assert pdm_private_registry_context([pyproject]) is False


def test_pdm_plain_source_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live (`pdm lock` under PDM 2.29.2, real unreachable
    `127.0.0.1:9` source): a `[[tool.pdm.source]]` table with no
    include_packages/exclude_packages is genuinely contacted for every
    dependency, not just ones matching some pattern."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n'
        "\n[[tool.pdm.source]]\n"
        'name = "private"\n'
        'url = "https://pypi.internal.example/simple"\n'
    )

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_include_packages_does_not_narrow_the_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live: setting `include_packages` on a source does not stop
    it from also being consulted for a name that doesn't match the pattern
    — PDM's own `_source_preference` only *adds* an exclusive claim for
    matching names, it never removes the source's default candidacy for
    everything else. Still blanket, same as the plain case above."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n'
        "\n[[tool.pdm.source]]\n"
        'name = "private"\n'
        'url = "https://pypi.internal.example/simple"\n'
        'include_packages = ["myorg-*"]\n'
    )

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_missing_file_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert pdm_private_registry_context([tmp_path / "pyproject.toml"]) is False


def test_pdm_malformed_toml_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("not valid toml [[[")

    assert pdm_private_registry_context([pyproject]) is False


def test_pdm_non_dict_tool_pdm_section_is_ignored(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n\n[tool]\npdm = "not-a-table"\n')

    assert pdm_private_registry_context([pyproject]) is False


def test_pdm_non_list_source_is_ignored(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n\n[tool.pdm]\nsource = "not-a-list"\n')

    assert pdm_private_registry_context([pyproject]) is False


def _clear_pdm_env(monkeypatch) -> None:
    for var in ("PDM_PYPI_URL", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS"):
        monkeypatch.delenv(var, raising=False)


def test_pdm_env_var_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live: `PDM_PYPI_URL=http://127.0.0.1:9/simple pdm lock`, with
    zero `[[tool.pdm.source]]` anywhere and no config file at all, genuinely
    attempted that address instead of pypi.org (PDM_PYPI_URL is `pypi.url`'s
    own documented env-var equivalent, read straight from PDM's own
    `_config_map`)."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PDM_PYPI_URL", "http://127.0.0.1:9/simple")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_env_var_set_to_public_pypi_is_not_blanket(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PDM_PYPI_URL", "https://pypi.org/simple")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')

    assert pdm_private_registry_context([pyproject]) is False


def test_pdm_project_local_pdm_toml_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live: a committed `pdm.toml` at the project root with
    `[pypi] url = ...` and zero `[[tool.pdm.source]]` anywhere in
    pyproject.toml made a real `pdm lock` genuinely attempt a connection to
    that address instead of pypi.org."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')
    (tmp_path / "pdm.toml").write_text('[pypi]\nurl = "http://127.0.0.1:9/simple"\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_legacy_dot_pdm_toml_is_blanket(tmp_path: Path, monkeypatch):
    """PDM still merges in the legacy `.pdm.toml` (pre-`pdm.toml`) project
    config file for backward compatibility -- confirmed by reading PDM
    2.29.2's own `Project.project_config` directly."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')
    (tmp_path / ".pdm.toml").write_text('[pypi]\nurl = "http://127.0.0.1:9/simple"\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_global_config_toml_is_blanket(tmp_path: Path, monkeypatch):
    """Confirmed live: `pdm config -g pypi.url ...` writes
    `$XDG_CONFIG_HOME/pdm/config.toml` (or `~/.config/pdm/config.toml`), and
    a real `pdm lock` with zero project-level config anywhere genuinely
    honored it the same way."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    config_dir = tmp_path / "xdgconfig"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    (config_dir / "pdm").mkdir(parents=True)
    (config_dir / "pdm" / "config.toml").write_text('[pypi]\nurl = "http://127.0.0.1:9/simple"\n')
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_global_config_falls_back_to_home_when_xdg_unset(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_file = home / ".config" / "pdm" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('[pypi]\nurl = "http://127.0.0.1:9/simple"\n')
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_site_config_toml_is_blanket(tmp_path: Path, monkeypatch):
    """PDM's own `Config.site` reads `platformdirs.site_config_path("pdm")`,
    which honors `$XDG_CONFIG_DIRS` (falling back to `/etc/xdg`) the same
    way `_pip_site_config_dirs` already does for pip."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    site_dir = tmp_path / "customsite"
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(site_dir))
    (site_dir / "pdm").mkdir(parents=True)
    (site_dir / "pdm" / "config.toml").write_text('[pypi]\nurl = "http://127.0.0.1:9/simple"\n')
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_config_pypi_url_set_to_public_pypi_is_not_blanket(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')
    (tmp_path / "pdm.toml").write_text('[pypi]\nurl = "https://pypi.org/simple"\n')

    assert pdm_private_registry_context([pyproject]) is False


def test_pdm_config_named_extra_source_is_blanket(tmp_path: Path, monkeypatch):
    """`pdm config pypi.<name>.url <url>` (confirmed live) writes a
    `[pypi.<name>]` sub-table -- an additional, PDM-own named source
    distinct from overriding the single default `pypi.url`."""
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["acmecorp-internal-widget"]\n')
    (tmp_path / "pdm.toml").write_text('[pypi.myrepo]\nurl = "http://127.0.0.1:9/simple"\n')

    assert pdm_private_registry_context([pyproject]) is True


def test_pdm_config_file_missing_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')

    assert pdm_private_registry_context([pyproject]) is False


def test_pdm_config_file_malformed_is_not_an_error(tmp_path: Path, monkeypatch):
    _clear_pdm_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["requests"]\n')
    (tmp_path / "pdm.toml").write_text("not valid toml [[[")

    assert pdm_private_registry_context([pyproject]) is False
