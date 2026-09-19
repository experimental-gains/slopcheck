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


def parse_requirements_txt(path: Path) -> list[Dependency]:
    deps = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "-e ", "--", "-c ")):
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


def _poetry_deps(data: dict) -> list[str]:
    names = []
    poetry = data.get("tool", {}).get("poetry", {})
    for section in ("dependencies", "dev-dependencies"):
        for name in poetry.get(section, {}):
            if name.lower() == "python":
                continue
            names.append(name)
    for group in poetry.get("group", {}).values():
        for name in group.get("dependencies", {}):
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


def parse_package_json(path: Path) -> list[Dependency]:
    data = json.loads(path.read_text())
    names = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names.extend(data.get(section, {}).keys())
    return [Dependency(name, "npm", str(path)) for name in names]


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
