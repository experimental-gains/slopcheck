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

_PIP_DIRECTIVE_RE = re.compile(r"^(?:-i|--(?:index-url|extra-index-url|pypi-url)\b)")


def _pip_config_paths() -> list[Path]:
    paths = []
    env_path = os.environ.get("PIP_CONFIG_FILE")
    if env_path:
        paths.append(Path(env_path))
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        paths.append(Path(virtual_env) / "pip.conf")
    paths.append(Path.home() / ".config" / "pip" / "pip.conf")
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


def _npmrc_paths(project_roots: list[Path]) -> list[Path]:
    paths = [root / ".npmrc" for root in project_roots]
    paths.append(Path.home() / ".npmrc")
    return paths


def npm_private_registry_context(project_roots: list[Path]) -> tuple[bool, set[str]]:
    """Returns (blanket_override, scopes_mapped_to_a_private_registry).

    A scope mapping (`@acme:registry=...`) only affects packages under that
    scope, mirroring GOPRIVATE's prefix scoping fairly closely; a blanket
    `registry=` override (or `npm_config_registry` env var) affects every
    package, like pip's extra-index-url.
    """
    blanket = bool(os.environ.get("npm_config_registry") or os.environ.get("NPM_CONFIG_REGISTRY"))
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
    return blanket, scopes


def npm_scope(name: str) -> str | None:
    if not name.startswith("@") or "/" not in name:
        return None
    return name.split("/", 1)[0]
