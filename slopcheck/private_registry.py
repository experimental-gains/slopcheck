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
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .parsers import _normalize_name

_PIP_DIRECTIVE_RE = re.compile(r"^(?:-i|--(?:index-url|extra-index-url|pypi-url)\b)")


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


def _pip_config_paths() -> list[Path]:
    paths = []
    env_path = os.environ.get("PIP_CONFIG_FILE")
    if env_path:
        paths.append(Path(env_path))
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        paths.append(Path(virtual_env) / "pip.conf")
    paths.append(_pip_user_config_dir() / "pip.conf")
    paths.append(Path.home() / ".pip" / "pip.conf")
    paths.append(Path("/etc/pip.conf"))
    return paths


def _pip_config_has_extra_index() -> bool:
    for path in _pip_config_paths():
        if not path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path)
        except configparser.Error:
            continue
        for section in ("global", "install"):
            if parser.has_option(section, "extra-index-url") or parser.has_option(section, "index-url"):
                return True
    return False


def pip_private_index_configured(requirements_txt_paths: list[Path]) -> bool:
    """Whether pip, run for real, would consult something beyond public PyPI.

    Unlike GOPRIVATE, pip's extra/alternate index isn't scoped to specific
    package name prefixes — it applies to every package pip resolves in that
    run — so this is a single yes/no for the whole scan rather than a
    per-name pattern match.
    """
    if os.environ.get("PIP_EXTRA_INDEX_URL") or os.environ.get("PIP_INDEX_URL"):
        return True
    if _pip_config_has_extra_index():
        return True
    for path in requirements_txt_paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text().splitlines():
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


def npm_private_registry_context(project_roots: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket_override, scopes_mapped_to_a_private_registry).

    A scope mapping (`@acme:registry=...`) only affects packages under that
    scope, mirroring GOPRIVATE's prefix scoping fairly closely; a blanket
    `registry=` override (or `npm_config_registry` env var) affects every
    package, like pip's extra-index-url. Yarn Berry projects can configure
    the same thing entirely independently, via `.yarnrc.yml`/
    `YARN_NPM_REGISTRY_SERVER` rather than `.npmrc`/`npm_config_registry` —
    see `_parse_yarnrc_registries` and `_yarnrc_paths`.
    """
    blanket = bool(_env_ci("npm_config_registry") or _env_ci("YARN_NPM_REGISTRY_SERVER"))
    scopes: set[str] = set()
    for rc_path in _npmrc_paths(project_roots):
        if not rc_path.is_file():
            continue
        for raw_line in rc_path.read_text().splitlines():
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
            text = rc_path.read_text()
        except OSError:
            continue
        yarn_blanket, yarn_scopes = _parse_yarnrc_registries(text)
        blanket = blanket or yarn_blanket
        scopes |= yarn_scopes
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


def npm_scope(name: str) -> str | None:
    if not name.startswith("@") or "/" not in name:
        return None
    return name.split("/", 1)[0]
