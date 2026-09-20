"""Extract dependency names from common manifest files.

Only the package name is needed (not version constraints) since the
whole point is checking whether the name exists in a registry at all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


class Dependency(NamedTuple):
    name: str
    ecosystem: str  # "pypi" or "npm"
    source: str  # file the dependency was found in


_REQ_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;].*)?$"
)

# pip treats a `#` as starting a trailing comment as long as it's preceded by
# whitespace (or starts the line) — e.g. "requests  # http client". Without
# stripping this, a plain unpinned dependency followed by an explanatory
# comment fails the regex above (it isn't a version specifier) and gets
# silently dropped instead of checked, which is a real, common style in
# hand-written requirements.txt files.
_INLINE_COMMENT_RE = re.compile(r"(?:^|\s)#")


def _strip_inline_comment(line: str) -> str:
    match = _INLINE_COMMENT_RE.search(line)
    return line[: match.start()].rstrip() if match else line


def parse_requirements_txt(path: Path) -> list[Dependency]:
    deps = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "-e ", "--", "-c ")):
            continue
        line = _strip_inline_comment(line)
        if not line:
            continue
        if "://" in line:
            continue
        match = _REQ_LINE_RE.match(line)
        if match:
            deps.append(Dependency(match.group("name"), "pypi", str(path)))
    return deps


def _pep621_deps(data: dict) -> list[str]:
    names = []
    project = data.get("project", {})
    for spec in project.get("dependencies", []):
        names.append(spec)
    for group in project.get("optional-dependencies", {}).values():
        names.extend(group)
    return names


def _is_poetry_registry_dep(spec) -> bool:
    """Whether a Poetry dependency spec resolves against PyPI at all.

    Poetry's table form (https://python-poetry.org/docs/dependency-specification/)
    lets a dependency point at a git remote, a local path, or an arbitrary
    URL instead of PyPI — the same non-registry-source situation as npm's
    workspace:/file:/git: protocols above, and just as common for internal/
    private packages in a monorepo. A plain version string, or a
    multiple-constraints list where at least one entry is a real registry
    version, still belongs on PyPI and should be checked; a table (or a
    list where every entry) specifying git/path/url does not.
    """
    if isinstance(spec, dict):
        return not any(key in spec for key in ("git", "path", "url"))
    if isinstance(spec, list):
        return any(_is_poetry_registry_dep(item) for item in spec)
    return True


def _poetry_deps(data: dict) -> list[str]:
    names = []
    poetry = data.get("tool", {}).get("poetry", {})
    for section in ("dependencies", "dev-dependencies"):
        for name, spec in poetry.get(section, {}).items():
            if name.lower() == "python":
                continue
            if not _is_poetry_registry_dep(spec):
                continue
            names.append(name)
    for group in poetry.get("group", {}).values():
        for name, spec in group.get("dependencies", {}).items():
            if not _is_poetry_registry_dep(spec):
                continue
            names.append(name)
    return names


def parse_pyproject_toml(path: Path) -> list[Dependency]:
    data = tomllib.loads(path.read_text())
    raw_specs = _pep621_deps(data) + _poetry_deps(data)
    deps = []
    for spec in raw_specs:
        match = _REQ_LINE_RE.match(spec.strip())
        if match:
            deps.append(Dependency(match.group("name"), "pypi", str(path)))
    return deps


# Version protocols that point at something other than the public registry
# (a workspace sibling, a local path, a git remote). Names under these
# protocols are never expected to resolve on npm even when legitimate —
# monorepo tooling (pnpm/Yarn/npm workspaces, Turborepo, Nx, Lerna) commonly
# names internal-only packages this way, so checking them against the
# registry produces a false "not found" on every workspace monorepo.
_NON_REGISTRY_PREFIXES = ("workspace:", "file:", "link:", "portal:", "git:", "git+", "github:")

_NPM_ALIAS_PREFIX = "npm:"


def _npm_alias_target(spec: str) -> str:
    """Resolve the real package name behind an `npm:` alias spec.

    `"local-name": "npm:real-name@1.2.3"` installs `real-name` under the
    key `local-name` — a legitimate npm/pnpm/Yarn feature for depending on
    a package under a different local name (renames, multiple versions of
    the same package side by side). Unlike workspace:/file:/git:, the
    target here *is* a registry name and can itself be hallucinated, so it
    needs to be extracted and checked rather than skipped like the other
    protocols.
    """
    rest = spec[len(_NPM_ALIAS_PREFIX):]
    at = rest.find("@", 1) if rest.startswith("@") else rest.find("@")
    return rest[:at] if at != -1 else rest


def parse_package_json(path: Path) -> list[Dependency]:
    data = json.loads(path.read_text())
    deps = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, version in data.get(section, {}).items():
            if isinstance(version, str) and version.startswith(_NPM_ALIAS_PREFIX):
                name = _npm_alias_target(version)
            elif isinstance(version, str) and version.startswith(_NON_REGISTRY_PREFIXES):
                continue
            deps.append(Dependency(name, "npm", str(path)))
    return deps


PARSERS = {
    "requirements.txt": parse_requirements_txt,
    "pyproject.toml": parse_pyproject_toml,
    "package.json": parse_package_json,
}


def find_manifests(root: Path) -> list[Path]:
    found = []
    for filename in PARSERS:
        candidate = root / filename
        if candidate.is_file():
            found.append(candidate)
    return found


def parse_manifest(path: Path) -> list[Dependency]:
    parser = PARSERS[path.name]
    return parser(path)
