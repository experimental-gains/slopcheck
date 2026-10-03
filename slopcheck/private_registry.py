"""Detect whether a private/extra package index is configured for this scan.

pip and npm both let a project route some or all dependency resolution to a
registry other than the public one (an internal index for private-only
packages) via config that lives outside the manifest file itself — pip.conf
or `PIP_EXTRA_INDEX_URL`/`--extra-index-url`, npm's `.npmrc` scope-to-
registry mapping. Confirmed live (real `pip install`/`npm install` against a
throwaway local index) that both actually resolve such packages successfully
even though they don't exist on the public registry slopcheck queries.

slopcheck can't authenticate to an arbitrary private registry to verify a
name really exists there, so — mirroring how this org's Go tools treat
GOPRIVATE/GONOPROXY-configured module paths — a name that isn't found on the
public registry but could plausibly be resolved from a private index
configured here should be reported as unverified, not flagged as a
hallucination.
"""
from __future__ import annotations

import configparser
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .parsers import (
    _normalize_name,
    npm_workspace_root,
    pdm_workspace_root,
    uv_workspace_root,
)

# `-f`/`--find-links` (a flat file/HTML-page/local-directory source of
# archives, searched *in addition to* the configured index rather than
# instead of it) is a genuinely separate way for a real `pip install` to
# resolve a name slopcheck can't see on public PyPI, not just a cosmetic
# alternative spelling of `-i`/`--extra-index-url`. Confirmed live (pip
# 25.1.1, a real wheel built for a never-published name and dropped in a
# throwaway local directory): a requirements.txt with `-f
# /path/to/local-wheels` as its first line and an unqualified
# `totally-hallucinated-findlinks-xyz-123==1.0.0` on the next line made
# `pip install --dry-run -r` genuinely find and "Would install" that exact
# version from the local directory ("Looking in links: /path/to/local-wheels"),
# with the public PyPI JSON API returning a plain 404 for the same name the
# whole time -- both the spaced (`-f /path`) and pip's own documented
# concatenated-short-option (`-f/path`) and `--find-links=/path` forms
# resolved identically. A local/internal wheelhouse reachable only this way
# (airgapped CI, a vendored-wheels directory committed alongside the project)
# is a real, current pattern distinct from a PEP 503 package index -- before
# this fix, slopcheck had no notion of it at all and reported a dependency
# resolvable this way as a plain `not_found` hallucination, the same
# misclassification already fixed here for `-i`/`--extra-index-url`, pip.conf,
# and `PIP_EXTRA_INDEX_URL`/`PIP_INDEX_URL`, just for this one still-uncovered
# real pip directive.
_PIP_DIRECTIVE_RE = re.compile(r"^(?:-i|-f|--(?:index-url|extra-index-url|pypi-url|find-links)\b)")


def _env_ci(name: str) -> str | None:
    """Case-insensitive environment variable lookup, npm/Yarn-only.

    npm and Yarn Berry both match their env-derived config keys fully
    case-insensitively — confirmed live with real `npm`/`yarn`:
    `Npm_Config_Registry`, `NPM_config_Registry`,
    `Yarn_Npm_Registry_Server`, and `Yarn_Rc_Filename` were all genuinely
    honored, not just the plain-lowercase and SCREAMING_CASE spellings
    this file used to check for each of these five vars. pip is the
    opposite — confirmed live that `Pip_Index_Url` is silently ignored by
    real `pip` (only the exact `PIP_INDEX_URL` spelling works) — so pip's
    own env var checks elsewhere in this file are deliberately exact-case
    and untouched by this helper.
    """
    target = name.lower()
    for key, value in os.environ.items():
        if key.lower() == target:
            return value
    return None


def _pip_user_config_dir() -> Path:
    """Where pip looks for its "new" per-user config file, on Linux.

    Mirrors pip's own resolution exactly (pip._internal.utils.appdirs.
    user_config_dir -> pip._vendor.platformdirs.unix.Unix.user_config_dir):
    `$XDG_CONFIG_HOME/pip` when XDG_CONFIG_HOME is set to a non-blank value,
    falling back to `~/.config/pip` only otherwise — confirmed live with
    `pip config -v list`: setting XDG_CONFIG_HOME replaces the `~/.config`
    lookup outright rather than adding to it, so a private index configured
    via `$XDG_CONFIG_HOME/pip/pip.conf` (a common pattern for anyone using a
    dotfiles manager or otherwise customizing XDG_CONFIG_HOME) was
    previously invisible to `_pip_config_has_extra_index` — the hardcoded
    `~/.config/pip/pip.conf` path this tool checked isn't the file pip
    itself would actually read, so a legitimately private-only dependency
    resolvable by a real `pip install` in that environment got reported as
    a plain `not_found` hallucination instead of downgraded to `private`.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg.strip():
        return Path(xdg) / "pip"
    return Path.home() / ".config" / "pip"


def _pip_site_config_dirs() -> list[Path]:
    """Where pip looks for its system-wide ("global") config file(s), on Linux.

    Mirrors pip's own resolution exactly (pip._internal.utils.appdirs.
    site_config_dirs -> pip._vendor.platformdirs.unix.Unix._site_config_dirs):
    every directory named by `$XDG_CONFIG_DIRS` (colon-separated, falling back
    to the single default `/etc/xdg` when unset *or* blank — the same
    non-blank-value rule `_pip_user_config_dir` already applies to
    `XDG_CONFIG_HOME`) gets `/pip` appended, and each resulting directory is
    checked for `pip.conf` — in *addition* to, not instead of, the separate
    hardcoded `/etc/pip.conf` this file already checks (pip's own
    `get_configuration_files` appends a bare `"/etc"` to this same list,
    joined with the config basename the same way `_pip_config_paths` already
    does).

    Confirmed live with real pip, two ways: (1) with no environment
    customization at all — this box's real, unmodified default — a
    `/etc/xdg/pip/pip.conf` setting `extra-index-url` made `pip install
    --dry-run -v` against a fake package name genuinely show "Looking in
    indexes: https://pypi.org/simple, http://127.0.0.1:9/simple", proof pip
    reads this location by default, not just under a hypothetical XDG setup;
    (2) with `XDG_CONFIG_DIRS` set to a custom path, the same pip.conf placed
    at `$XDG_CONFIG_DIRS/pip/pip.conf` instead was read the same way, with
    `/etc/xdg/pip/pip.conf` no longer consulted (`platformdirs.site_config_dir`
    confirmed via direct call to replace, not add to, the XDG default —
    `XDG_CONFIG_DIRS=/custom/xdg` genuinely produced `/custom/xdg/pip` and
    nothing else). Before this fix, `_pip_config_paths` only ever checked
    `/etc/pip.conf`, so a private index configured via this real, genuinely
    default pip config location (a real deployment pattern: a Linux distro or
    Docker base image dropping a managed pip.conf here, the *actual* place
    the "global" config pip's own docs promise, not merely a
    `/etc/pip.conf`-adjacent guess) was invisible to this tool — a
    genuinely private-only dependency misreported as a plain `not_found`
    hallucination instead of downgraded to `private`.
    """
    raw = os.environ.get("XDG_CONFIG_DIRS", "")
    dirs = raw.split(os.pathsep) if raw.strip() else ["/etc/xdg"]
    return [Path(d) / "pip" for d in dirs]


def _pip_config_paths() -> list[Path]:
    """pip's own "site" config variant is keyed off `sys.prefix`, not `$VIRTUAL_ENV`.

    Reading pip's own source (pip._internal.configuration.get_configuration_files:
    `site_config_file = os.path.join(sys.prefix, CONFIG_BASENAME)`, and
    `ConfigOptionParser`'s override order includes this "site" variant
    unconditionally, with no dependency on the `VIRTUAL_ENV` environment
    variable anywhere in that path) shows this file only checked
    `$VIRTUAL_ENV/pip.conf`, which is a real but incomplete proxy for it: the
    `VIRTUAL_ENV` env var is only set when a venv was *activated* (`source
    venv/bin/activate`) — a real, extremely common alternative is invoking a
    venv's own pip directly by path (`./venv/bin/pip install ...`, the
    standard pattern in Dockerfiles/CI/Makefiles that never sources
    activate), which leaves `VIRTUAL_ENV` unset entirely while `sys.prefix`
    inside that pip process is still the venv root.

    Confirmed live: created `/tmp/sc_venv`, installed slopcheck into it,
    dropped `[install] extra-index-url = http://127.0.0.1:9/simple` into
    `/tmp/sc_venv/pip.conf`, and ran both real pip and slopcheck *from that
    same venv* with `VIRTUAL_ENV` explicitly unset (`env -u VIRTUAL_ENV`).
    Real pip's own verbose output showed "Looking in indexes:
    https://pypi.org/simple, http://127.0.0.1:9/simple" — it genuinely read
    the venv-root pip.conf with no VIRTUAL_ENV involved at all, exactly as
    its own source predicts (sys.prefix inside that venv's Python is the venv
    directory regardless of whether it was "activated"). slopcheck, run the
    same way from a `slopcheck` console-script installed into that very
    venv, reported a hallucinated requirements.txt entry as a plain
    `not_found` instead of downgrading it to `private` — a real
    misclassification for the common case where slopcheck itself is
    installed into the same project venv it's scanning (e.g. `pip install -e
    .[dev]` covering both pytest and this tool) and invoked without
    activation, the same way its sibling pip would be.

    Checking `sys.prefix != sys.base_prefix` (the standard way to detect
    "the running interpreter is itself inside a venv", used by pip's own
    `sys.prefix`-based site file and by `venv`'s own activation-detection
    idiom) subsumes the already-correct `VIRTUAL_ENV`-activated case for
    free, since activating a venv makes any process launched from that shell
    (including slopcheck) inherit a matching `sys.prefix` — so both are kept
    rather than one replacing the other, covering the (separate, real) case
    where `VIRTUAL_ENV` names a *different* venv than the one slopcheck's own
    interpreter happens to be running under.
    """
    paths = []
    env_path = os.environ.get("PIP_CONFIG_FILE")
    if env_path:
        paths.append(Path(env_path))
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        paths.append(Path(virtual_env) / "pip.conf")
    if sys.prefix != sys.base_prefix:
        paths.append(Path(sys.prefix) / "pip.conf")
    paths.append(_pip_user_config_dir() / "pip.conf")
    paths.append(Path.home() / ".pip" / "pip.conf")
    paths.extend(site_dir / "pip.conf" for site_dir in _pip_site_config_dirs())
    paths.append(Path("/etc/pip.conf"))
    return paths


_PIP_INDEX_OPTION_NAMES = {"extra-index-url", "index-url", "find-links"}


def _pip_config_has_extra_index() -> bool:
    """Whether any of pip's config files sets an extra/alternate index, or a find-links source.

    Checks each option name in both pip.conf's canonical dash spelling and
    its underscore alias (`extra_index_url`/`index_url`/`find_links`) —
    confirmed live (`PIP_CONFIG_FILE=... pip config -v list`) that real pip's
    own config loader normalizes every key it reads with `name.lower().replace("_",
    "-")` (pip._internal.configuration._normalized_keys) before storing it,
    so `extra_index_url = ...` in pip.conf is exactly as effective as
    `extra-index-url = ...` — and confirmed further (`pip install --dry-run
    -v`, unreachable `127.0.0.1:9` index) that a bare-underscore pip.conf
    genuinely made pip look in that index ("Looking in indexes:
    https://pypi.org/simple, http://127.0.0.1:9/simple"). configparser
    itself does no such normalization (only `option.lower()`, never
    underscore-to-dash), so checking only the two dash-spelled option names
    via `parser.has_option` silently missed a pip.conf written with the
    underscore spelling — a real, pip-documented-equivalent spelling, not an
    invalid one — misreporting a genuinely private-only dependency as a
    plain `not_found` hallucination instead of downgrading it to `private`.

    `find-links` (`[global] find-links = ...`) is included alongside the two
    index options for the same reason `_PIP_DIRECTIVE_RE` now matches `-f`/
    `--find-links` on a requirements.txt line — see that regex's own comment
    for the live-verified gap. Confirmed live the same way here too: a
    `pip.conf` with only `[global] find-links = /path/to/local-wheels` (no
    `index-url`/`extra-index-url` at all) made a real `pip install --dry-run
    -r` genuinely resolve and "Would install" a name that 404s on the public
    PyPI JSON API, straight from that local directory.
    """
    for path in _pip_config_paths():
        if not path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path)
        except configparser.Error:
            continue
        for section in ("global", "install"):
            if not parser.has_section(section):
                continue
            for option in parser.options(section):
                if option.replace("_", "-") in _PIP_INDEX_OPTION_NAMES:
                    return True
    return False


def pip_private_index_configured(
    requirements_txt_paths: list[Path], extra_lines: Sequence[str] = ()
) -> bool:
    """Whether pip, run for real, would consult something beyond public PyPI.

    Unlike GOPRIVATE, pip's extra/alternate index isn't scoped to specific
    package name prefixes — it applies to every package pip resolves in that
    run — so this is a single yes/no for the whole scan rather than a
    per-name pattern match.

    Reads each file as "utf-8-sig", not plain "utf-8" — confirmed live: a
    requirements.txt starting with a UTF-8 BOM (the same real, valid file
    shape `parsers._parse_requirements_txt` already handles this way, e.g.
    one saved by a Windows editor) with `-i http://127.0.0.1:9/simple` as
    its first line was genuinely honored by a real `pip install -r` —
    "Looking in indexes: http://127.0.0.1:9/simple" in pip's own output,
    followed by real connection attempts to that address for a
    fake-package name. Before this fix, `path.read_text()` (no encoding)
    left the leading `﻿` glued onto that first line, `_PIP_DIRECTIVE_RE`
    (anchored at the start of the line) no longer matched it, and this
    function silently returned `False` — misreporting a genuinely
    private-only dependency as a plain `not_found` hallucination instead of
    downgrading it to `private`, even though this same file's dependency
    names were already being read correctly (parsers.py has used
    "utf-8-sig" for exactly this reason since the BOM fix documented
    above it).

    `extra_lines` scans a set of already-read line strings the same way, in
    addition to `requirements_txt_paths`' own files. Needed for conda's
    `environment.yml`: its `pip:` block is a real, pip-directive-bearing
    requirement list (see `parsers.environment_yml_pip_lines`'s docstring —
    conda writes it verbatim into a temp requirements file for a real `pip
    install -r` subprocess), but it lives inside a YAML file's body, not as
    its own `.txt`/`.in` path this function could just read directly the way
    it does for every other source here.

    `PIP_FIND_LINKS` is pip's own env var equivalent of a requirements-file
    `-f`/`--find-links` line (see `_PIP_DIRECTIVE_RE`'s own comment for the
    live-verified gap this whole find-links handling closes) — confirmed
    live the same way: with no `-f` anywhere in any scanned file, setting
    only `PIP_FIND_LINKS=/path/to/local-wheels` in the environment still made
    a real `pip install --dry-run -r` resolve a name that 404s on public
    PyPI straight from that directory.
    """
    if (
        os.environ.get("PIP_EXTRA_INDEX_URL")
        or os.environ.get("PIP_INDEX_URL")
        or os.environ.get("PIP_FIND_LINKS")
    ):
        return True
    if _pip_config_has_extra_index():
        return True
    for path in requirements_txt_paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            if _PIP_DIRECTIVE_RE.match(raw_line.strip()):
                return True
    for raw_line in extra_lines:
        if _PIP_DIRECTIVE_RE.match(raw_line.strip()):
            return True
    return False


_NPMRC_SCOPE_RE = re.compile(r"^(@[^:=\s]+):registry\s*=")
_NPMRC_BLANKET_RE = re.compile(r"^registry\s*=")


def _npm_global_config_paths() -> list[Path]:
    # npm also reads a machine-wide "global" config file, beneath the
    # per-user one in precedence but still consulted for any key the
    # project/user files don't set — confirmed live with a real
    # `npm install` against a throwaway scope: a scope-to-registry mapping
    # *or* a blanket `registry=` override placed only in the global config
    # is genuinely honored, no project/user npmrc involved at all. This is
    # a real deployment pattern: an org baking a private-registry mapping
    # into a CI runner or Docker base image at the machine level, keeping
    # every project's and every user's own npmrc untouched.
    #
    # Its location can be relocated the same way as `userconfig`, via an
    # `npm_config_globalconfig`/`NPM_CONFIG_GLOBALCONFIG` env var — confirmed
    # live, same case-insensitive resolution as userconfig.
    override = _env_ci("npm_config_globalconfig")
    if override:
        return [Path(override)]
    # With no override, npm derives this from its own install-time global
    # --prefix, which has no single portable default — it depends on how
    # node/npm was installed. Confirmed live on this box: Debian/Ubuntu's
    # packaged npm ships a builtin config hardcoding `globalconfig=/etc/npmrc`
    # (a common Docker/CI base). npm's own source documents its own
    # non-Debian-patched default example as "the global --prefix setting
    # plus 'etc/npmrc' ... for example, '/usr/local/etc/npmrc'" (the
    # standard result when --prefix defaults to /usr/local, e.g. an
    # official installer/nvm/Homebrew-on-Linux install). Rather than guess
    # one, check both well-known static locations, the same shape
    # `_pip_config_paths` already uses for `/etc/pip.conf`.
    return [Path("/etc/npmrc"), Path("/usr/local/etc/npmrc")]


def _npmrc_paths(project_roots: list[Path]) -> list[Path]:
    # npm's per-user config file defaults to ~/.npmrc, but `userconfig`
    # (like every other npm config key) can itself be set via an
    # `npm_config_userconfig` env var and relocate it — confirmed live
    # (`npm_config_userconfig=/path/to/file npm config get <key>` actually
    # reads that file instead of ~/.npmrc, case-insensitively: lowercase,
    # uppercase, and mixed-case env var spellings all worked). A scope
    # mapping placed only in that relocated file (a real pattern for
    # anyone managing dotfiles/CI images with a non-default npmrc
    # location, the npm analog of the already-fixed pip XDG_CONFIG_HOME
    # gap below) was previously invisible here, so a legitimately
    # private-only npm dependency got reported as a plain `not_found`
    # hallucination instead of downgraded to `private`.
    user_config = _env_ci("npm_config_userconfig")
    paths = [root / ".npmrc" for root in project_roots]
    # A project root that's actually an npm/Yarn-Classic workspace *member*
    # (not the workspace root itself) reads its enclosing workspace root's
    # `.npmrc` too — confirmed live, see `npm_workspace_root`'s docstring.
    # Without this, scanning a workspace member directory on its own (its
    # own `.npmrc`, if any, already covered by the entry above) never saw
    # the root's scope/blanket config at all.
    for root in project_roots:
        workspace_root = npm_workspace_root(root)
        if workspace_root is not None:
            paths.append(workspace_root / ".npmrc")
    paths.append(Path(user_config) if user_config else Path.home() / ".npmrc")
    paths.extend(_npm_global_config_paths())
    return paths


_YARNRC_KEY_RE = re.compile(r"^([^:\s][^:]*):\s*(.*)$")


def _yarnrc_filename() -> str:
    # Like npm's userconfig, Yarn Berry's per-directory config filename can
    # itself be relocated via an env var — confirmed live
    # (`YARN_RC_FILENAME=custom.yarnrc.yml yarn install` genuinely read
    # `custom.yarnrc.yml` instead of the default `.yarnrc.yml`, using its
    # `npmScopes` entry to route a resolution attempt at the private
    # registry configured there). Applies at every level Yarn searches
    # (project directory and the home-directory global one below), same as
    # the real algorithm.
    return _env_ci("YARN_RC_FILENAME") or ".yarnrc.yml"


def _yarnrc_paths(project_roots: list[Path]) -> list[Path]:
    # Yarn Berry (v2+) doesn't read `.npmrc` at all for its own registry
    # config — it's a separate, YAML config file, `.yarnrc.yml`, found at
    # the project root and (confirmed live: a `~/.yarnrc.yml` with no
    # project-level file at all was genuinely picked up and honored,
    # routing resolution at the registry it named) merged with a
    # home-directory global one, mirroring `_npmrc_paths`' project+user
    # split above but for a package manager `_npmrc_paths` never covers.
    filename = _yarnrc_filename()
    paths = [root / filename for root in project_roots]
    paths.append(Path.home() / filename)
    return paths


def _parse_yarnrc_registries(text: str) -> tuple[bool, set[str]]:
    """Extract (blanket_override, scope_names) from a `.yarnrc.yml`'s content.

    Not a general YAML parser — Yarn always writes (and real-world files
    consistently use) plain block-style mappings for these two keys, so a
    small indentation-aware line walker is enough, the same "hand-parse the
    subset of syntax that matters" approach `_npmrc_paths` already takes for
    `.npmrc` (also not a real INI file, parsed with regex rather than
    `configparser`). Flow-style (`npmScopes: {foo: {npmRegistryServer: ...}}`)
    is a known gap, not worth the added complexity for a form Yarn's own
    tooling never generates.

    Confirmed live with a real `yarn install` against a throwaway local
    registry: both a top-level `npmRegistryServer:` (blanket, every package)
    and a `npmScopes.<name>.npmRegistryServer:` (scoped) entry are genuinely
    honored — the resolution step visibly tried to reach the configured
    address instead of the public npm registry in both cases, as did the
    `YARN_NPM_REGISTRY_SERVER` env var equivalent of the blanket form
    (checked separately in `npm_private_registry_context`, no file to parse).
    """
    blanket = False
    scopes: set[str] = set()
    in_npm_scopes = False
    scope_key_indent: int | None = None
    current_scope: str | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _YARNRC_KEY_RE.match(stripped)
        if not match:
            continue
        key, value = match.group(1).strip("\"'"), match.group(2).strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if indent == 0:
            in_npm_scopes = key == "npmScopes"
            current_scope = None
            scope_key_indent = None
            if key == "npmRegistryServer" and value:
                blanket = True
            continue

        if not in_npm_scopes:
            continue

        if scope_key_indent is None:
            scope_key_indent = indent
        if indent == scope_key_indent:
            current_scope = key
        elif current_scope and indent > scope_key_indent and key == "npmRegistryServer" and value:
            # `npmScopes` keys are bare scope names ("acme", not "@acme") —
            # confirmed live, `yarn install` matched a dependency's `@acme/...`
            # name against a `npmScopes.acme` entry. `npm_scope()` returns the
            # `@`-prefixed form, so store it that way here too for the two to
            # compare equal in `_downgrade_if_private`.
            scopes.add(f"@{current_scope}")

    return blanket, scopes


_YARN_CLASSIC_RC_LINE_RE = re.compile(r'^(?:"(?P<quoted_key>[^"]+)"|(?P<bare_key>[^\s"]+))\s+"(?P<value>[^"]*)"\s*$')

_YARN_CLASSIC_RC_SCOPE_KEY_RE = re.compile(r"^(@[^:\s]+):registry$")


def _yarn_classic_config_home() -> Path:
    # Yarn Classic (v1) deliberately relocates its *global* config-home
    # directory away from the real home directory when running as root —
    # confirmed by reading the actual `yarn` 1.22.22 npm package's bundled
    # `cli.js` directly (the `root-user`/`user-home` helper modules it
    # ships): `isRootUser(process.getuid()) && !isFakeRoot() ?
    # path.resolve('/usr/local/share') : os.homedir()` (`isFakeRoot` only
    # checks a `FAKEROOTKEY` env var, a `fakeroot(1)` escape hatch), and
    # this relocated path — not `$HOME` — is what every global `.yarnrc`
    # lookup (`homeConfigLoc`) is actually built from. Confirmed live
    # running real Yarn Classic 1.22.22 as root (`process.getuid() === 0`,
    # no `FAKEROOTKEY`): a blanket `registry "..."` entry placed *only* in
    # `/usr/local/share/.yarnrc` — no project-level `.yarnrc`, no
    # `$HOME/.yarnrc` at all — was genuinely honored by a real
    # `yarn install --verbose` (a GET to the configured address,
    # ECONNREFUSED, never touching `registry.yarnpkg.com`). Root is an
    # extremely common way to run both real installs (Docker/CI base
    # images) and slopcheck itself, so `Path.home()` alone silently missed
    # this for the majority of containerized real-world runs.
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not os.environ.get("FAKEROOTKEY"):
        return Path("/usr/local/share")
    return Path.home()


def _yarn_classic_rc_paths(project_roots: list[Path]) -> list[Path]:
    # Yarn Classic (v1) has a *third* independent registry-config file,
    # distinct from both `.npmrc` (`_npmrc_paths` above) and Yarn Berry's
    # YAML `.yarnrc.yml` (`_yarnrc_paths` above): its own `.yarnrc`, in a
    # plain (non-YAML) `key "value"` format — confirmed live (Yarn Classic
    # 1.22.22): a scratch project with *only* a `.yarnrc` (no `.npmrc`, no
    # `.yarnrc.yml` anywhere) containing `registry "http://127.0.0.1:9/"`
    # made `yarn install --verbose` genuinely perform a GET against
    # `http://127.0.0.1:9/<name>` and fail with ECONNREFUSED, never
    # contacting `registry.yarnpkg.com` at all. Read from the project root
    # and merged with a global one, the same project+global split
    # `_npmrc_paths`/`_yarnrc_paths` already use — confirmed live too: a
    # blanket `registry "..."` placed only in the global file, with no
    # project-level `.yarnrc` at all, was genuinely honored the same way.
    # The global file's own location isn't always `~/.yarnrc` — see
    # `_yarn_classic_config_home`.
    paths = [root / ".yarnrc" for root in project_roots]
    # Same workspace-root gap `_npmrc_paths` fixes, for Yarn Classic's own
    # `.yarnrc` — confirmed live (Yarn Classic 1.22.22): `yarn config get`
    # run from inside a workspace member directory walked all the way up to
    # the enclosing workspace root looking for `.yarnrc`/`.npmrc` (verbose
    # log: "Checking for configuration file" at every ancestor level) and
    # genuinely used a `registry`/scope directive found only there, with no
    # `.yarnrc` of its own in the member directory at all. `npm_workspace_root`
    # is reused rather than a from-scratch unconditional ancestor walk (real
    # Yarn Classic's own walk isn't gated on a `workspaces` field match at
    # all) to keep this to the specific, common monorepo shape already
    # confirmed rather than risk a false `private` downgrade from an
    # unrelated `.yarnrc` sitting somewhere above an ordinary, non-workspace
    # nested project.
    for root in project_roots:
        workspace_root = npm_workspace_root(root)
        if workspace_root is not None:
            paths.append(workspace_root / ".yarnrc")
    paths.append(_yarn_classic_config_home() / ".yarnrc")
    return paths


def _parse_yarn_classic_rc(text: str) -> tuple[bool, set[str]]:
    """Extract (blanket_override, scope_names) from a Yarn Classic `.yarnrc`'s content.

    Unlike Yarn Berry's YAML `.yarnrc.yml`, Yarn Classic's own `.yarnrc` is a
    flat sequence of `key "value"` lines — exactly what `yarn config set
    registry <url>` itself writes, and the same shape as the autogenerated
    header every real `.yarnrc` this tool will encounter carries. A bare key
    (`registry`) needs no quoting; a key containing a colon (the scoped form,
    `"@acme:registry"`) is always quoted in a real file since `:` isn't a
    valid bare-token character here. Confirmed live that the *value* must be
    double-quoted for Yarn Classic to honor it at all: an otherwise-identical
    unquoted `registry http://127.0.0.1:9/` line was silently ignored by a
    real `yarn install` (it fell straight through to the public registry),
    and a single-quoted `registry 'http://127.0.0.1:9/'` line was ignored the
    same way — only the double-quoted form Yarn's own tooling writes is
    matched here, so a line real Yarn itself wouldn't honor doesn't get
    mistaken for a private-registry signal either.
    """
    blanket = False
    scopes: set[str] = set()
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _YARN_CLASSIC_RC_LINE_RE.match(stripped)
        if not match:
            continue
        key = match.group("quoted_key") or match.group("bare_key")
        if key == "registry":
            blanket = True
            continue
        scope_match = _YARN_CLASSIC_RC_SCOPE_KEY_RE.match(key)
        if scope_match:
            scopes.add(scope_match.group(1))
    return blanket, scopes


def npm_private_registry_context(project_roots: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket_override, scopes_mapped_to_a_private_registry).

    A scope mapping (`@acme:registry=...`) only affects packages under that
    scope, mirroring GOPRIVATE's prefix scoping fairly closely; a blanket
    `registry=` override (or `npm_config_registry` env var) affects every
    package, like pip's extra-index-url. Yarn Berry projects can configure
    the same thing entirely independently, via `.yarnrc.yml`/
    `YARN_NPM_REGISTRY_SERVER` rather than `.npmrc`/`npm_config_registry` —
    see `_parse_yarnrc_registries` and `_yarnrc_paths`. Yarn Classic (v1)
    has a *third*, independent mechanism of its own, its own plain-format
    `.yarnrc` plus a `YARN_REGISTRY` env var — confirmed live case-
    insensitively the same way `npm_config_registry`/`YARN_NPM_REGISTRY_
    SERVER` already are (`Yarn_Registry=...` was genuinely honored too) —
    see `_parse_yarn_classic_rc` and `_yarn_classic_rc_paths`. Before this
    fix, neither the file nor the env var was read at all, so a Yarn-
    Classic-only project routing dependencies through a private registry
    this way had every genuinely-resolvable private dependency reported as
    a plain `not_found` hallucination.

    Both files are read as "utf-8-sig", not plain "utf-8" — confirmed live,
    the same BOM behavior `pip_private_index_configured` was just fixed for
    above: a `.npmrc` starting with a UTF-8 BOM and a bare `@acme:registry=
    http://127.0.0.1:9/` line was genuinely honored by a real `npm install`
    (verbose log showed a real connection attempt to that address for a
    `@acme/`-scoped fake package, not the public registry), and a BOM'd
    `.yarnrc.yml` with `npmRegistryServer: "http://127.0.0.1:9"` was
    likewise honored by real Yarn Berry (4.18.1) — it refused the resolution
    with "Unsafe http requests must be explicitly whitelisted ... (127.0.0.1)",
    which only happens once it has actually read that address out of the
    config. Plain "utf-8" leaves the leading `﻿` glued onto each file's
    first line, so a scope/registry directive placed there (a common spot)
    silently failed to match and this function returned a false blanket=False/
    scopes=set() — misreporting a genuinely private-only npm/Yarn dependency
    as a plain `not_found` hallucination instead of downgrading it to
    `private`.
    """
    blanket = bool(
        _env_ci("npm_config_registry") or _env_ci("YARN_NPM_REGISTRY_SERVER") or _env_ci("YARN_REGISTRY")
    )
    scopes: set[str] = set()
    for rc_path in _npmrc_paths(project_roots):
        if not rc_path.is_file():
            continue
        for raw_line in rc_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            scope_match = _NPMRC_SCOPE_RE.match(line)
            if scope_match:
                scopes.add(scope_match.group(1))
            elif _NPMRC_BLANKET_RE.match(line):
                blanket = True
    for rc_path in _yarnrc_paths(project_roots):
        if not rc_path.is_file():
            continue
        try:
            text = rc_path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        yarn_blanket, yarn_scopes = _parse_yarnrc_registries(text)
        blanket = blanket or yarn_blanket
        scopes |= yarn_scopes
    for rc_path in _yarn_classic_rc_paths(project_roots):
        if not rc_path.is_file():
            continue
        try:
            text = rc_path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        classic_blanket, classic_scopes = _parse_yarn_classic_rc(text)
        blanket = blanket or classic_blanket
        scopes |= classic_scopes
    return blanket, scopes


def _bunfig_paths(project_roots: list[Path]) -> list[Path]:
    # Bun (https://bun.sh/docs/install/registries) reads its own config file,
    # `bunfig.toml`, at the project root, entirely independent of `.npmrc` —
    # confirmed live (Bun 1.4.2): a project with only a `bunfig.toml` (no
    # `.npmrc` at all) genuinely routed `bun install` at a private registry,
    # both for a blanket `[install].registry` override and a scoped
    # `[install.scopes]` entry, each confirmed as a real connection attempt
    # at the configured address (ConnectionRefused to a closed local port)
    # rather than the public npm registry (a 404 there instead). Bun also
    # reads a project-root `.npmrc`'s scope/blanket directives on top of its
    # own `bunfig.toml` (confirmed live too), so that case is already covered
    # by `npm_private_registry_context` without any Bun-specific code — this
    # function only needs to add the config Bun alone reads.
    #
    # A user-level `~/.bunfig.toml` is read too (confirmed live, same
    # ConnectionRefused tell, with no project-root bunfig.toml present at
    # all), the Bun analogue of npm's userconfig / Yarn's home-directory
    # `.yarnrc.yml` fallback.
    paths = [root / "bunfig.toml" for root in project_roots]
    paths.append(Path.home() / ".bunfig.toml")
    return paths


def bunfig_private_registry_context(project_roots: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket, scopes_mapped_to_a_private_registry) for Bun's own config.

    `[install].registry` (a plain URL string, or a `{ url = ..., token = ...
    }` table for an authenticated registry — confirmed live, both forms
    genuinely redirect resolution) is Bun's blanket override, and
    `BUN_CONFIG_REGISTRY` is its env var equivalent (confirmed live, mirrors
    npm's `npm_config_registry`). `[install.scopes]` keys are already
    `@`-prefixed in real `bunfig.toml` files (unlike Yarn's bare-name
    `npmScopes` keys), so they need no reformatting to compare equal to
    `npm_scope()`'s output — confirmed live for both a plain-string and a
    `{ url = ..., token = ... }` scope value, either shape genuinely
    redirects that scope alone.
    """
    blanket = bool(os.environ.get("BUN_CONFIG_REGISTRY"))
    scopes: set[str] = set()
    for path in _bunfig_paths(project_roots):
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        install = data.get("install", {})
        if not isinstance(install, dict):
            continue
        if install.get("registry"):
            blanket = True
        install_scopes = install.get("scopes", {})
        if isinstance(install_scopes, dict):
            scopes.update(install_scopes.keys())
    return blanket, scopes


def poetry_private_registry_context(pyproject_paths: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket, names_pinned_to_an_explicit_source).

    Poetry's own `[[tool.poetry.source]]` table (https://python-poetry.org/
    docs/repositories/#project-configuration) is a *third* private-registry
    mechanism, entirely separate from pip's config/env vars that
    `pip_private_index_configured` reads: a Poetry-managed project routes
    dependency resolution through its own source list at `poetry lock`/
    `poetry install` time, independent of any `pip.conf`/`PIP_INDEX_URL`,
    which usually isn't set at all in a pure-Poetry environment.

    Confirmed live (real `poetry lock` against a throwaway unreachable
    `https://127.0.0.1:9/simple/` source, three priority levels): a source
    with no `priority` key, or `priority = "primary"` or `"supplemental"`,
    is genuinely consulted for *any* dependency not otherwise pinned to it
    -- with no priority set at all, Poetry disables the default PyPI source
    outright ("Adding repository ... and setting it as primary. Deactivating
    the PyPI repository."); with `"supplemental"`, PyPI is tried first (a
    real 404 for a nonexistent name) and the supplemental source is tried
    next regardless. Either way, a name absent from public PyPI can still
    resolve for a real `poetry install` -- the same blanket-override shape
    pip's extra-index-url already gets folded into `pip_private`.
    `priority = "explicit"` is different and scoped, mirroring npm's scope
    mapping: confirmed live that an explicit source is *never* consulted
    unless a specific dependency opts in via its own `source = "<name>"`
    key (an unreferenced explicit source left the fake dependency a
    same-shape `SolverProblemError` -- PyPI-only, `127.0.0.1:9` never even
    contacted -- while a dependency that *did* reference it went straight to
    that source and only that source).
    """
    blanket = False
    explicit_names: set[str] = set()

    for path in pyproject_paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue

        poetry = data.get("tool", {}).get("poetry", {})
        sources = poetry.get("source", [])
        if not isinstance(sources, list):
            continue

        explicit_source_names = set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            name = source.get("name")
            if not name:
                continue
            if source.get("priority") == "explicit":
                explicit_source_names.add(name)
            else:
                blanket = True

        if not explicit_source_names:
            continue

        dep_tables = [poetry.get(section, {}) for section in ("dependencies", "dev-dependencies")]
        dep_tables.extend(group.get("dependencies", {}) for group in poetry.get("group", {}).values())
        for dep_table in dep_tables:
            for dep_name, spec in dep_table.items():
                # A "multiple constraints" dependency (Poetry's own documented
                # syntax for varying a dependency's spec by python-version/
                # platform marker) is a *list* of tables, not one table --
                # confirmed live (`poetry lock`) that a platform-scoped variant
                # naming an explicit source is genuinely honored (the resolver
                # contacted that source's URL, not PyPI, for the matching
                # platform) exactly like the single-table case below. Reading
                # only `spec.get("source")` missed this shape entirely, since
                # a list has no `.get` -- `isinstance(spec, dict)` was False
                # for every entry and the whole dependency silently never
                # scoped, producing a false `not_found` hallucination flag for
                # a name that a real `poetry install` resolves fine on the
                # matching platform.
                specs = spec if isinstance(spec, list) else [spec]
                if any(isinstance(s, dict) and s.get("source") in explicit_source_names for s in specs):
                    explicit_names.add(_normalize_name(dep_name))

    return blanket, explicit_names


_PUBLIC_PYPI_URLS = {
    "https://pypi.org/simple",
    "http://pypi.org/simple",
    "https://pypi.python.org/simple",
    "http://pypi.python.org/simple",
}


def _is_public_pypi_url(url: str) -> bool:
    return url.rstrip("/").lower() in _PUBLIC_PYPI_URLS


def pipfile_private_registry_context(pipfile_paths: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket, names_pinned_to_a_non-default_source), for Pipenv's `Pipfile`.

    Pipenv's own `[[source]]` array (https://pipenv.pypa.io/en/latest/
    specifiers/#specifying-package-indexes) is a *fourth* private-registry
    mechanism, structurally different from Poetry's `[[tool.poetry.source]]`
    that `poetry_private_registry_context` already covers: there's no
    `priority` key at all. Confirmed live (real `pipenv lock` under Pipenv
    2026.8.0, three source arrangements, watching for the `127.0.0.1:9`
    connection attempt as the tell — same unreachable-source technique
    `poetry_private_registry_context` used):

    - With no per-package `index` key, a dependency resolves *only* against
      the first `[[source]]` entry in file order — not the entry named
      "pypi" specifically, and not every source. A second, later source
      (even an unreachable one) is never contacted for that dependency.
      This means a project whose first source's `url` is a private mirror
      (a real, common pattern: an org replacing the default entirely,
      keeping the conventional `name = "pypi"` pipenv itself always
      generates) routes *every* undecorated dependency there — the same
      blanket-override shape as pip's `--index-url` and Poetry's
      non-explicit sources.
    - A dependency spec's own `index = "<name>"` key scopes it to exactly
      that named source and no other — confirmed live going straight to
      the private URL, the public-PyPI-named source never contacted for
      that name at all, mirroring Poetry's `source = "<name>"` explicit
      scoping.

    Before this, slopcheck had zero awareness of this mechanism at all —
    neither the blanket nor the per-package form — so any Pipfile-based
    project using either pattern got every genuinely-private-only name it
    named reported as a plain `not_found` hallucination instead of
    downgraded to `private`.

    `[[source]]` is mandatory boilerplate every real Pipfile carries (Pipenv
    always writes one, unlike Poetry's optional source table), so unlike
    `poetry_private_registry_context` this can't treat "any source present"
    as the private signal — that would flag the overwhelming majority of
    ordinary, pure-public-PyPI Pipfiles as blanket-private and silently
    swallow every real hallucination in them. The first source's `url` value
    itself is compared against the known public-PyPI URLs instead.

    `PIPENV_PYPI_MIRROR` is a *fifth*, entirely separate mechanism, outside
    the Pipfile itself: reading Pipenv's own source
    (`pipenv/utils/sources.py`'s `pipfile_sources`), when this env var is
    set it overwrites the `url` of every source — including the implicit
    built-in default used when a Pipfile has no `[[source]]` table at all —
    whose current `url` matches `is_pypi_url` (`^https?://pypi(\\.python)?
    \\.org/simple/?$`, the same public-PyPI URL set `_is_public_pypi_url`
    already recognizes). Confirmed live (real `pipenv lock` under Pipenv
    2026.8.0, two arrangements, watching for the `127.0.0.1:9` connection
    attempt): a Pipfile with the conventional `name = "pypi", url =
    "https://pypi.org/simple"` as its only source, and a Pipfile with *no*
    `[[source]]` table at all, both genuinely routed an undecorated
    dependency's resolution to `PIPENV_PYPI_MIRROR`'s address instead of
    pypi.org — the exact mirror/caching-proxy pattern pip's own
    `PIP_INDEX_URL` and PDM's `PDM_PYPI_URL` already get folded into
    `blanket` elsewhere in this file. Before this fix, neither case was
    recognized at all, so a Pipenv project run with this env var set (a
    real, documented pattern — an offline/airgapped CI image or a
    company-wide caching mirror baked into the environment rather than
    repeated per-project) had every genuinely-resolvable dependency
    reported as a plain `not_found` hallucination instead of downgraded to
    `private`.
    """
    blanket = False
    explicit_names: set[str] = set()

    mirror = os.environ.get("PIPENV_PYPI_MIRROR")
    mirror_replaces_public = bool(mirror) and not _is_public_pypi_url(mirror)

    for path in pipfile_paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue

        sources = data.get("source", [])
        if not isinstance(sources, list) or not sources:
            # No `[[source]]` table at all: Pipenv's own implicit default
            # source is public PyPI, which PIPENV_PYPI_MIRROR replaces the
            # same as an explicit one — see this function's own docstring.
            if mirror_replaces_public:
                blanket = True
            continue

        source_urls = {
            source["name"]: source.get("url", "")
            for source in sources
            if isinstance(source, dict) and source.get("name")
        }

        default_url = sources[0].get("url", "") if isinstance(sources[0], dict) else ""
        if default_url and not _is_public_pypi_url(default_url):
            blanket = True
        elif mirror_replaces_public and _is_public_pypi_url(default_url):
            blanket = True

        for section in ("packages", "dev-packages"):
            for dep_name, spec in data.get(section, {}).items():
                if not isinstance(spec, dict):
                    continue
                index_name = spec.get("index")
                if not index_name:
                    continue
                index_url = source_urls.get(index_name, "")
                if index_url and not _is_public_pypi_url(index_url):
                    explicit_names.add(_normalize_name(dep_name))

    return blanket, explicit_names


_UV_ENV_BLANKET_VARS = ("UV_INDEX", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS")


def _uv_config_paths(project_roots: list[Path]) -> list[Path]:
    # uv is a *fifth* private-registry mechanism (https://docs.astral.sh/uv/
    # concepts/indexes/), structurally closest to Poetry's: a `[[tool.uv.
    # index]]` array of named index tables, each either blanket (consulted
    # for every dependency, the default when `explicit` is omitted/false) or
    # `explicit = true` (only consulted for a dependency whose `[tool.uv.
    # sources]` entry names it via `index = "<name>"`). Unlike Poetry, this
    # config can live outside pyproject.toml entirely, in a standalone
    # `uv.toml` — confirmed live (real `uv lock` against an unreachable
    # `127.0.0.1:9` index, connection-refused as the tell, same technique
    # `poetry_private_registry_context`/`pipfile_private_registry_context`
    # already used): a project-root `uv.toml`, and a user-level one, are
    # both genuinely read and honored with zero pyproject.toml involvement.
    # `UV_CONFIG_FILE` relocates the search entirely to one explicit path,
    # confirmed live the same way — mirrors pip's `PIP_CONFIG_FILE`.
    override = os.environ.get("UV_CONFIG_FILE")
    if override:
        return [Path(override)]
    paths = [root / "uv.toml" for root in project_roots]
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    # Confirmed live via `uv -v lock`'s own "Searching for user
    # configuration in: ..." debug line: falls back to `~/.config/uv/
    # uv.toml` when XDG_CONFIG_HOME is unset *or* blank, same
    # non-blank-value rule as `_pip_user_config_dir` above.
    base = Path(xdg) if xdg.strip() else Path.home() / ".config"
    paths.append(base / "uv" / "uv.toml")
    return paths


def _uv_index_entries(data: dict) -> list[dict]:
    entries = data.get("index", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _uv_has_find_links(data: dict) -> bool:
    # `find-links` (https://docs.astral.sh/uv/reference/settings/#find-links)
    # is uv's own analog of pip's `-f`/`--find-links`: a list of flat file/
    # HTML-page/local-directory sources searched *in addition to* the
    # configured index(es) — the same real, separate-from-any-index
    # resolution path `_PIP_DIRECTIVE_RE`'s own comment documents for pip,
    # just reachable through uv's config instead of a requirements.txt line
    # or pip.conf. Confirmed live (uv 0.12.19, a hand-built wheel for a
    # never-published name dropped in a throwaway local directory, no
    # `[[tool.uv.index]]`/`uv.toml` index entry anywhere): both a
    # `find-links = [...]` key directly in a standalone `uv.toml` and the
    # identical key inside pyproject.toml's `[tool.uv]` table made `uv lock`
    # genuinely resolve that exact name straight from the local directory,
    # with real PyPI (`uv lock` run online, no find-links) failing the same
    # name with "was not found in the package registry". Unlike `[[tool.uv.
    # index]]`, this key has no `explicit`/scoped form at all -- every entry
    # applies to the whole resolution, the same blanket shape as pip's
    # `find-links`, so this only ever contributes to `blanket`, never to a
    # per-name scoped set.
    return bool(data.get("find-links"))


def _uv_has_deprecated_index_url(data: dict) -> bool:
    """Whether uv's older, pip-compatible `index-url`/`extra-index-url` keys are set.

    Before uv grew the `[[tool.uv.index]]` array (what `_uv_index_entries`
    reads), it modeled a blanket default/extra index the same flat way pip
    does: a single `index-url = "..."` string and an `extra-index-url =
    [...]` list, settable at the top level of either a standalone `uv.toml`
    or a pyproject.toml's `[tool.uv]` table. uv's own settings reference
    marks both "Deprecated: use `index` instead" — deprecated, not removed
    or ignored, and `_uv_index_entries`/`uv_private_registry_context` never
    read either one, only the newer array form.

    Confirmed live with real uv 0.12.19, three ways, all genuinely issuing a
    GET for a dependency name that 404s on the real public PyPI JSON API
    against `http://127.0.0.1:9/simple` (connection-refused, the same tell
    every other live-verification in this file uses) instead of leaving it
    unresolved: a pyproject.toml's `[tool.uv]` table with only `index-url`
    set and no `[[tool.uv.index]]` entry at all; the same table with only
    `extra-index-url` set; and a standalone `uv.toml` with only `index-url`
    set and zero `[tool.uv]` section in pyproject.toml. A negative control
    with neither key set anywhere resolved the identical dependency-name
    shape as a plain 404, confirming the redirection was genuinely caused by
    these keys and not some other default. Before this fix, a project using
    either deprecated key (a real, still-current pattern — `uv`'s own docs
    keep documenting it as a working pip-compatible shorthand, e.g. for
    pinning a PyTorch CPU wheel index) had every genuinely-resolvable
    private/extra dependency reported as a plain `not_found` hallucination
    instead of downgraded to `private`. Like `_uv_has_find_links`, this has
    no `explicit`/scoped form — both keys always apply to the whole
    resolution, so this only ever contributes to `blanket`.
    """
    return bool(data.get("index-url")) or bool(data.get("extra-index-url"))


def uv_private_registry_context(pyproject_paths: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket, names pinned to an explicit uv index).

    Confirmed live: when a project directory has *both* a `uv.toml` and a
    `[tool.uv]` section in its `pyproject.toml`, uv prints a warning and the
    `uv.toml` file's `index` field wins outright — pyproject.toml's
    `[tool.uv.index]` is not consulted at all in that case. This reads both
    unconditionally and merges their index names rather than modeling that
    precedence exactly, the same over-approximation `pip_private_index_
    configured` already makes by treating a directive found in *any* file
    as applying to the whole scan: the failure mode of wrongly downgrading
    a name to `private` (unverified) when it wasn't actually reachable is
    far preferable to wrongly flagging a genuinely-installable private-only
    dependency as a hallucination.

    `[tool.uv.sources]` (the per-dependency `index = "<name>"` scoping key)
    is unaffected by a sibling `uv.toml`'s presence either way — confirmed
    live, the warning names only `index` among `[tool.uv]`'s fields as
    overridden — so it's always read from pyproject.toml regardless of
    which file supplied the matching index *name*.

    A `find-links` key in either file is also checked, folding into
    `blanket` the same way `UV_FIND_LINKS` already does below — see
    `_uv_has_find_links`'s own comment for the live-verified gap this closes
    (uv's own analog of pip's `-f`/`--find-links`, previously unhandled here
    even though the equivalent pip directive was already fixed).

    uv's older, deprecated-but-still-functional `index-url`/`extra-index-url`
    keys (the pip-compatible flat predecessor of `[[tool.uv.index]]`) are
    also checked in either file, folding into `blanket` the same way —
    see `_uv_has_deprecated_index_url`'s own comment for the live-verified
    gap this closes.

    A scanned pyproject.toml that's actually a uv workspace *member* (not
    the workspace root itself) also pulls in its enclosing workspace root's
    `uv.toml`/`[tool.uv]` config — confirmed live, see `uv_workspace_root`'s
    own docstring for the gap this closes (the uv analog of
    `npm_workspace_root`'s identical fix for npm/Yarn). Every `pyproject.toml`
    this function reads (a scanned path's own, plus any workspace root found
    this way) is parsed once into `parsed_uv_tables` up front, so the later
    per-dependency `[tool.uv.sources]` scoping pass always sees the complete
    `explicit_index_names` set regardless of which file — root or member —
    happened to declare the matching `[[tool.uv.index]]` entry, rather than
    depending on `pyproject_paths`' own, otherwise-arbitrary iteration order.
    """
    blanket = any(os.environ.get(var) for var in _UV_ENV_BLANKET_VARS)
    explicit_index_names: set[str] = set()
    explicit_dep_names: set[str] = set()

    def _collect(entries: list[dict]) -> None:
        nonlocal blanket
        for entry in entries:
            if entry.get("explicit"):
                name = entry.get("name")
                if name:
                    explicit_index_names.add(name)
            else:
                blanket = True

    project_roots = [p.parent for p in pyproject_paths]
    workspace_roots = {root for root in (uv_workspace_root(r) for r in project_roots) if root is not None}
    all_roots = list(dict.fromkeys([*project_roots, *workspace_roots]))
    all_pyproject_paths = list(
        dict.fromkeys([*pyproject_paths, *(root / "pyproject.toml" for root in workspace_roots)])
    )

    for path in _uv_config_paths(all_roots):
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        _collect(_uv_index_entries(data))
        if _uv_has_find_links(data):
            blanket = True
        if _uv_has_deprecated_index_url(data):
            blanket = True

    parsed_uv_tables: list[dict] = []
    for path in all_pyproject_paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        uv = data.get("tool", {}).get("uv", {})
        if not isinstance(uv, dict):
            continue
        parsed_uv_tables.append(uv)
        _collect(_uv_index_entries(uv))
        if _uv_has_find_links(uv):
            blanket = True
        if _uv_has_deprecated_index_url(uv):
            blanket = True

    if explicit_index_names:
        for uv in parsed_uv_tables:
            sources = uv.get("sources", {})
            if not isinstance(sources, dict):
                continue
            for dep_name, spec in sources.items():
                specs = spec if isinstance(spec, list) else [spec]
                if any(isinstance(s, dict) and s.get("index") in explicit_index_names for s in specs):
                    explicit_dep_names.add(_normalize_name(dep_name))

    return blanket, explicit_dep_names


def _pdm_config_paths(project_roots: list[Path]) -> list[Path]:
    """Every file PDM itself reads for `pypi.*` settings, outside pyproject.toml.

    PDM's own `pdm config` command (https://pdm-project.org/en/latest/usage/
    config/#configuring-pypi-index) writes `pypi.url`/`pypi.<name>.url`
    settings to one of *three* TOML files, none of which is pyproject.toml
    and none of which `pdm_private_registry_context` read before this fix:

    - a project-local `pdm.toml` at the project root (`pdm config -l`,
      read directly from PDM 2.29.2's own `Project.project_config`:
      `Config(self.root / "pdm.toml")`), plus the legacy `.pdm.toml` PDM
      still merges in for backward compatibility (same source, the
      `# TODO: for backward compatibility` block right below it) —
      confirmed live: a from-scratch project with *only* a committed
      `pdm.toml` containing `[pypi] url = "http://127.0.0.1:9/simple"` (no
      `[[tool.pdm.source]]` anywhere in pyproject.toml) made `pdm lock`
      genuinely attempt a connection to that address instead of
      `pypi.org` (`ConnectError: [Errno 111] Connection refused`, the same
      ConnectionRefused tell `poetry_private_registry_context`'s own
      live-verification already uses).
    - a per-user global config (`pdm config -g`), at
      `platformdirs.user_config_path("pdm") / "config.toml"` — confirmed by
      reading PDM's own source (`Config.site`/`get_defaults` in
      `pdm/project/config.py`) and live: `platformdirs.user_config_path`
      resolves to `$XDG_CONFIG_HOME/pdm` when that var is set to a
      non-blank value, `~/.config/pdm` otherwise (the identical
      non-blank-value XDG rule `_pip_user_config_dir`/`_uv_config_paths`
      already apply for pip/uv's own per-user config), and a `pypi.url`
      set only there (zero project-level config at all) was genuinely
      honored by a real `pdm lock` the same way.
    - a machine-wide config PDM's own `Config.site` reads from
      `platformdirs.site_config_path("pdm") / "config.toml"`, which
      resolves every directory in `$XDG_CONFIG_DIRS` (falling back to the
      single default `/etc/xdg` when unset or blank) the same way
      `_pip_site_config_dirs` already does for pip's own `/etc/xdg/pip`.

    Before this fix, none of these three files -- nor the `PDM_PYPI_URL`
    env var `pdm_private_registry_context` now also checks -- were read at
    all, so a PDM project routing its default index through any of them
    (a real, current, documented PDM feature, not a hypothetical one) had
    every genuinely-resolvable private-only dependency reported as a plain
    `not_found` hallucination -- the exact same blanket-override shape
    already fixed here for pip's `pip.conf`/`PIP_INDEX_URL` and uv's
    standalone `uv.toml`/`UV_INDEX_URL`, just never ported to PDM's own,
    structurally distinct config-file mechanism.
    """
    paths = [root / "pdm.toml" for root in project_roots]
    paths.extend(root / ".pdm.toml" for root in project_roots)
    xdg_home = os.environ.get("XDG_CONFIG_HOME", "")
    user_base = Path(xdg_home) if xdg_home.strip() else Path.home() / ".config"
    paths.append(user_base / "pdm" / "config.toml")
    xdg_dirs_raw = os.environ.get("XDG_CONFIG_DIRS", "")
    site_dirs = xdg_dirs_raw.split(os.pathsep) if xdg_dirs_raw.strip() else ["/etc/xdg"]
    paths.extend(Path(d) / "pdm" / "config.toml" for d in site_dirs)
    return paths


def _pdm_config_file_has_custom_source(path: Path) -> bool:
    """Whether a PDM config TOML file (not pyproject.toml) names a non-default index.

    PDM's `load_config` (`pdm/project/config.py`) flattens a nested
    `[pypi]`/`[pypi.<name>]` table into dotted keys (`pypi.url`,
    `pypi.<name>.url`, ...) -- `pdm config pypi.url <url>` writes the
    former (overriding the single default index every dependency falls
    back to), `pdm config pypi.<name>.url <url>` the latter (an
    additional, PDM-own named source, confirmed live via `pdm config -g
    pypi.myrepo.url ...` writing a `[pypi.myrepo]` sub-table). Either
    shape is checked here the same conservative, whole-file way
    `pdm_private_registry_context`'s `[[tool.pdm.source]]` check already
    is: no attempt to model PDM's own per-name `include_packages`/
    `exclude_packages` scoping (see that function's own docstring for why
    that's deliberately left as a blanket signal instead).

    A `pypi.url` set to a known-public PyPI mirror is compared against
    `_is_public_pypi_url` the same way `pipfile_private_registry_context`
    already treats Pipenv's own default-source URL, so simply re-stating
    the real default in a committed `pdm.toml` (a harmless, real pattern
    -- `pdm config -l pypi.url https://pypi.org/simple`) doesn't itself
    trigger a blanket `private` downgrade for an otherwise perfectly
    ordinary project.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    pypi = data.get("pypi")
    if not isinstance(pypi, dict):
        return False
    url = pypi.get("url")
    if isinstance(url, str) and url and not _is_public_pypi_url(url):
        return True
    for key, value in pypi.items():
        if key == "url":
            continue
        if isinstance(value, dict) and isinstance(value.get("url"), str) and value["url"]:
            return True
    return False


def pdm_private_registry_context(pyproject_paths: list[Path]) -> bool:
    """Whether PDM, run for real, would consult something beyond public PyPI.

    PDM's own `[[tool.pdm.source]]` array (https://pdm-project.org/en/latest/
    usage/config/#specify-another-index-for-installation) is a *sixth*
    private-registry mechanism, structurally its own: unlike Poetry's
    `priority = "explicit"` or Pipenv's per-dependency `index =` key, PDM
    scopes a source to specific packages via glob patterns on the source
    itself (`include_packages`/`exclude_packages`), not a field on the
    dependency spec.

    Confirmed live (real `pdm lock` under PDM 2.29.2, against an unreachable
    `http://127.0.0.1:9/simple` source, connection-refused as the tell): a
    source with no `include_packages`/`exclude_packages` at all is genuinely
    contacted for *every* dependency, alongside the default PyPI index — the
    same blanket-by-default shape as Pipenv's `[[source]]`. Setting
    `include_packages = ["myorg-*"]` alone does **not** stop the source from
    still being consulted for a name that doesn't match the pattern (verified
    live: a fake, non-matching name still hit `127.0.0.1:9`) — reading PDM's
    own `filtered_sources`/`_source_preference` (pdm/utils.py), a pattern
    match only *adds* an exclusive claim on matching names (excluding PyPI
    for those), it doesn't *remove* the source's default candidacy for
    everything else. The only way a source stops applying to a given name is
    an `exclude_packages` pattern that matches it.

    That per-name exclude carve-out isn't modeled here — it would need every
    already-parsed dependency name threaded through just to except a rarely-
    used pattern, for a still-safe failure mode (over-reporting `private`
    instead of a genuine hallucination) this file already accepts elsewhere
    (see `uv_private_registry_context`). So this mirrors `pip_private_index_
    configured`'s plain whole-scan bool: any `[[tool.pdm.source]]` table
    present at all means PDM could resolve a name beyond public PyPI for this
    scan.

    `PDM_PYPI_URL` (https://pdm-project.org/en/latest/usage/config/#common-configuration-items,
    PDM's own `pypi.url` config item's documented env-var equivalent, read
    directly from `pdm/project/config.py`'s `_config_map`) and the three
    `pdm config`-written TOML files `_pdm_config_paths` checks are a
    *seventh*, entirely separate way PDM routes resolution beyond public
    PyPI, with zero `[[tool.pdm.source]]` (or any other pyproject.toml
    table) involved at all — see `_pdm_config_paths`'/`_pdm_config_file_
    has_custom_source`'s own docstrings for the live-verified gap this
    closes. Confirmed live the env var works the same way: `PDM_PYPI_URL=
    http://127.0.0.1:9/simple pdm lock`, zero config files anywhere,
    genuinely attempted that address instead of `pypi.org`.

    A scanned pyproject.toml that's actually a PDM workspace *member* (not
    the workspace root itself) also pulls in its enclosing workspace root's
    `[[tool.pdm.source]]`/`pdm.toml`/`.pdm.toml` — confirmed live, see
    `pdm_workspace_root`'s own docstring for the gap this closes (the PDM
    analog of `uv_workspace_root`'s identical fix for uv, run #133/#646):
    a root-level `[[tool.pdm.source]]` pointed at an unreachable address
    made a real `pdm lock` — run from the root, the only way PDM allows it
    to run at all — genuinely attempt that address for a dependency
    declared only in the *member's* own `[project.dependencies]`, with zero
    `[tool.pdm.source]` of its own. Before this fix, scanning that member
    directory alone never saw the root's source table at all, so the
    dependency was reported a plain `not_found` hallucination instead of
    downgraded to `private`.
    """
    if os.environ.get("PDM_PYPI_URL") and not _is_public_pypi_url(os.environ["PDM_PYPI_URL"]):
        return True

    project_roots = [p.parent for p in pyproject_paths]
    workspace_roots = {root for root in (pdm_workspace_root(r) for r in project_roots) if root is not None}
    all_roots = list(dict.fromkeys([*project_roots, *workspace_roots]))
    all_pyproject_paths = list(
        dict.fromkeys([*pyproject_paths, *(root / "pyproject.toml" for root in workspace_roots)])
    )

    for path in _pdm_config_paths(all_roots):
        if path.is_file() and _pdm_config_file_has_custom_source(path):
            return True

    for path in all_pyproject_paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        pdm = data.get("tool", {}).get("pdm", {})
        if not isinstance(pdm, dict):
            continue
        sources = pdm.get("source", [])
        if isinstance(sources, list) and any(isinstance(s, dict) for s in sources):
            return True
    return False


def npm_scope(name: str) -> str | None:
    if not name.startswith("@") or "/" not in name:
        return None
    return name.split("/", 1)[0]
