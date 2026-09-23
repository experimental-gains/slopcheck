"""Extract dependency names from common manifest files.

Only the package name is needed (not version constraints) since the
whole point is checking whether the name exists in a registry at all.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


class ManifestParseError(Exception):
    """Raised when a manifest file can't be parsed as its expected format.

    Covers the file-is-corrupt/truncated/wrong-encoding case (invalid JSON,
    invalid TOML, invalid UTF-8) as opposed to a missing or unreadable file,
    which is checked earlier in cli.main. Wrapping the parser-specific
    exceptions here gives callers one exception type to catch and a message
    that names the offending file, instead of an unhandled traceback from
    deep inside json/tomllib.
    """


class Dependency(NamedTuple):
    name: str
    ecosystem: str  # "pypi" or "npm"
    source: str  # file the dependency was found in


# PEP 508 still allows the legacy parenthesized form of a version specifier
# (e.g. "numpy (>=1.16)", carried over from PEP 440/setup.py-style
# install_requires strings) as an alternative to the bare "numpy>=1.16"
# form. Without a branch for it, a dependency written that way didn't match
# at all and was silently dropped instead of checked — a real false
# negative, not just a cosmetic style difference, since `packaging`
# (pip's own requirement parser) accepts it.
_REQ_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*"
    r"(?:\([^)]*\)\s*(?:;.*)?|[<>=!~;].*)?$"
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


# All three manifest readers below use "utf-8-sig" rather than plain "utf-8":
# it transparently strips a leading UTF-8 byte-order mark when one is present
# and behaves identically to plain "utf-8" when it isn't, so it's a safe
# blanket default rather than a special case. A BOM-prefixed manifest is real,
# valid content real tooling handles (npm's own package.json reader strips it,
# and vitejs/vite's repo ships `playground/resolve/utf8-bom-package/
# package.json` specifically to test that bundlers resolve it correctly) —
# found via real-world testing against that exact file. Without this,
# `json.loads`/`tomllib.loads` raise on the leftover `﻿` and abort the
# whole scan (worse now that `find_manifests` recurses — see below — since
# one BOM'd file anywhere in a large tree kills every other manifest's
# results too), while the line-based `requirements.txt` reader doesn't crash
# but silently corrupts the *first* line into an unmatchable string, dropping
# that dependency from checking entirely with no error at all.
# pip's nested-requirements-file directive, both the short (`-r`) and long
# (`--requirement`) spellings. A real, common way of structuring
# requirements.txt across several files (e.g. Home Assistant's core repo:
# requirements_test.txt starts with "-r requirements_test_pre_commit.txt",
# cookiecutter-django's requirements/production.txt starts with "-r
# base.txt") — pip resolves the referenced path *relative to the file that
# references it*, not the CWD or the top-level file, and recurses into it
# for real (see pip's own docs on nested requirements files). Without
# following it, every dependency named only in the referenced file was
# silently never checked at all — the tool's core job failing silently on
# a standard, real-world requirements.txt composition pattern, not an edge
# case.
_REQ_FILE_RE = re.compile(r"^(?:-r|--requirement)\s+(?P<target>.+?)\s*$")

# pip's constraints-file directive (`-c`/`--constraint`). Deliberately NOT
# recursed into like `-r` above: per pip's own documentation, a name that
# appears *only* in a constraints file and nowhere else in the resolved
# requirement set has no effect at all — pip won't install it. Treating a
# constraints-only entry as a real dependency would risk a false positive
# (flagging a name that's merely a version pin for some other project's
# transitive dependency, never installed by this one) rather than fixing a
# false negative, so it stays skipped.
_CONSTRAINT_FILE_RE = re.compile(r"^(?:-c|--constraint)\s+(?P<target>.+?)\s*$")


def parse_requirements_txt(path: Path) -> list[Dependency]:
    return _parse_requirements_txt(path, set())


def _parse_requirements_txt(path: Path, seen: set[Path]) -> list[Dependency]:
    resolved = path.resolve()
    if resolved in seen:
        # A `-r` cycle (directly or indirectly self-referencing). Real pip
        # would also loop forever on this, but there's no reason to hang or
        # crash a name-existence check over a malformed requirements file —
        # just stop recursing into an already-visited file.
        return []
    seen = seen | {resolved}

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ManifestParseError(f"{path}: referenced requirements file not found ({e})") from e

    deps = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip a trailing comment before checking for -r/-c/-e/-- prefixes
        # too, not just before the name regex below — a referenced file can
        # carry an explanatory trailing comment the same way an ordinary
        # dependency line can (e.g. "-r base.txt  # shared deps"), and
        # leaving it on would fold the comment text into the target path.
        line = _strip_inline_comment(line)
        if not line:
            continue
        req_match = _REQ_FILE_RE.match(line)
        if req_match:
            target = path.parent / req_match.group("target")
            deps.extend(_parse_requirements_txt(target, seen))
            continue
        if _CONSTRAINT_FILE_RE.match(line) or line.startswith(("-e ", "--")):
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


def _dependency_groups_deps(data: dict) -> list[str]:
    """Flatten PEP 735 `[dependency-groups]` entries into requirement specs.

    `[dependency-groups]` (accepted 2024, supported by pip 24.3+, uv, hatch,
    PDM) is a top-level table *sibling* to `[project]`, not nested under it
    like `optional-dependencies` — real, current pyproject.toml files (uv's
    own, pytest's, pydantic's, fastapi's) all use it for dev/docs/test
    dependencies, and `_pep621_deps` never looked at it, so every name in
    it was silently never checked at all (not just mis-parsed — `uv`'s own
    pyproject.toml has zero `[project.dependencies]`, so its real dev/docs
    deps were 100% unchecked). Each group is a list of either a plain PEP
    508 requirement string, or an `{include-group = "other"}` table
    referencing another group's dependencies instead of naming a package
    itself. That reference doesn't need to be resolved/followed here: this
    function iterates every group in the table directly, so the group it
    points at contributes its own entries on its own turn through the loop
    — the reference is just skipped as it isn't a requirement spec.
    """
    names = []
    for group in data.get("dependency-groups", {}).values():
        for item in group:
            if isinstance(item, str):
                names.append(item)
    return names


_NAME_NORMALIZE_RE = re.compile(r"[-_.]+")


def _normalize_name(name: str) -> str:
    """PEP 503 name normalization: case- and separator-insensitive.

    Needed to match a `[tool.uv.sources]` key against a PEP 621/dependency-
    groups spec name reliably — real files write the same package with
    different casing/separators in the two places (uv itself, and pip,
    treat `Foo-Bar`, `foo_bar`, and `foo.bar` as the same distribution).
    """
    return _NAME_NORMALIZE_RE.sub("-", name).lower()


def _is_uv_registry_source(value) -> bool:
    """Whether a `[tool.uv.sources]` entry still resolves via a package index.

    uv (https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-sources)
    lets a `[project.dependencies]`/`optional-dependencies`/`dependency-groups`
    entry's *source* be overridden independently of its name — the same idea
    as Poetry's git/path/url table form (`_is_poetry_registry_dep` above) and
    npm's `workspace:`/`file:`/`git:` protocols, but for uv specifically,
    which has become one of the most common Python packaging/dependency tools
    in real current projects (litellm, crewAI, pydantic-ai, langflow, and
    many more all use it) and wasn't handled here at all. `git`/`path`/
    `workspace` sources bypass every package index entirely (`workspace`
    resolves to a local sibling package in the same repo, e.g. a docs- or
    tooling-only package that's never published — confirmed live against
    marimo-team/marimo's real pyproject.toml: `marimo_docs = { path =
    "./docs", editable = true }` under `[tool.uv.sources]`, with
    `marimo_docs` a bare entry in `[project.optional-dependencies].docs` and
    genuinely 404 on PyPI, both spellings). `url` points at one specific
    file, not an index either. `index` merely redirects to a *different*
    index (e.g. a PyTorch build's own package index) rather than opting out
    of index resolution, so those names still need checking. A value can
    also be a list of per-platform/marker source tables; mirrors
    `_is_poetry_registry_dep`'s multiple-constraints bias of treating the
    name as still-registry-resolvable if any entry is.
    """
    if isinstance(value, dict):
        return not any(key in value for key in ("git", "path", "workspace", "url"))
    if isinstance(value, list):
        return any(_is_uv_registry_source(item) for item in value)
    return True


def _uv_non_registry_names(data: dict) -> set[str]:
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    return {
        _normalize_name(name)
        for name, value in sources.items()
        if not _is_uv_registry_source(value)
    }


def parse_pyproject_toml(path: Path) -> list[Dependency]:
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    raw_specs = _pep621_deps(data) + _poetry_deps(data) + _dependency_groups_deps(data)
    skip_names = _uv_non_registry_names(data)
    deps = []
    for spec in raw_specs:
        match = _REQ_LINE_RE.match(spec.strip())
        if match:
            name = match.group("name")
            if _normalize_name(name) in skip_names:
                continue
            deps.append(Dependency(name, "pypi", str(path)))
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


def _strip_version_suffix(name: str) -> str:
    """Strip an optional trailing `@version`/`@range`/`@protocol` suffix.

    `"foo@1.0.0"` pins `foo` to that version — real npm overrides-key
    syntax (e.g. vscode's `package.json` uses `"kerberos@2.1.1"` to
    override `node-addon-api` only for that specific `kerberos`
    version) and also real Yarn `resolutions`-key syntax (e.g. jest's
    `package.json` uses `"lru-cache@^10.0.1"` and `"@types/mdx@npm:
    ^2.0.0"` to pin a patched resolution). Without stripping it, the
    version-and-all string gets checked against the registry as if it
    were the package name and never matches — even though the package
    itself is real and published.
    """
    at = name.find("@", 1) if name.startswith("@") else name.find("@")
    return name[:at] if at != -1 else name


def _npm_overrides_deps(overrides: dict) -> list[str]:
    """Flatten npm's `overrides` field into the package names it references.

    Each key names a package somewhere in the dependency tree; a string
    value pins its version (or, via an `npm:` alias, substitutes a
    different package entirely — same aliasing rule as regular
    dependencies above). A key can also map to a nested object instead of
    a version string — npm's syntax for "when resolving X's dependency on
    Y, use this" — where `"."` re-pins X itself and every other key is
    another package name one level further down the chain. Both the outer
    and nested keys are real package names that belong on npm and can
    just as easily be hallucinated as a top-level dependency.
    """
    names = []
    for name, value in overrides.items():
        if name == ".":
            continue
        if isinstance(value, str) and value.startswith(_NPM_ALIAS_PREFIX):
            names.append(_npm_alias_target(value))
        else:
            names.append(_strip_version_suffix(name))
        if isinstance(value, dict):
            names.extend(_npm_overrides_deps(value))
    return names


def _yarn_resolution_target(pattern: str) -> str:
    """Extract the package name from a Yarn `resolutions` pattern.

    A pattern is a `/`-separated path through the dependency tree (e.g.
    `webpack/**/ws` or `**/lodash`), optionally with `**` wildcard
    segments; the package actually being pinned is the last real segment.
    Scoped packages (`@babel/core`) contain their own `/`, so a trailing
    `@scope` segment is rejoined with the name segment after it. Yarn
    also allows a range/protocol pinned directly onto that last segment
    (`"lru-cache@^10.0.1"`, `"@types/mdx@npm:^2.0.0"` — both real, from
    jest's `package.json`), which needs the same suffix-stripping as an
    npm overrides key or it never matches the registry.
    """
    segments = [s for s in pattern.split("/") if s and s != "**"]
    if not segments:
        return pattern
    if len(segments) >= 2 and segments[-2].startswith("@"):
        return _strip_version_suffix(f"{segments[-2]}/{segments[-1]}")
    return _strip_version_suffix(segments[-1])


def parse_package_json(path: Path) -> list[Dependency]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    deps = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, version in data.get(section, {}).items():
            if isinstance(version, str) and version.startswith(_NPM_ALIAS_PREFIX):
                name = _npm_alias_target(version)
            elif isinstance(version, str) and version.startswith(_NON_REGISTRY_PREFIXES):
                continue
            deps.append(Dependency(name, "npm", str(path)))
    for name in _npm_overrides_deps(data.get("overrides", {})):
        deps.append(Dependency(name, "npm", str(path)))
    # pnpm has its own overrides field, nested under a top-level "pnpm" key
    # (`"pnpm": {"overrides": {...}}`) rather than npm's root-level
    # "overrides" — real, current usage: prisma/prisma's package.json uses
    # exactly this form (`pnpm.overrides`, six entries including
    # version-scoped keys like "minimatch@3.1.2"), and pnpm supports npm's
    # root-level "overrides" too, so the two fields are additive, not
    # either/or — a manifest can use both. Same value shape as npm's
    # overrides (a version string, an npm: alias, or a nested object), so
    # _npm_overrides_deps handles it unchanged.
    pnpm_overrides = data.get("pnpm", {})
    if isinstance(pnpm_overrides, dict):
        for name in _npm_overrides_deps(pnpm_overrides.get("overrides", {})):
            deps.append(Dependency(name, "npm", str(path)))
    for pattern in data.get("resolutions", {}):
        deps.append(Dependency(_yarn_resolution_target(pattern), "npm", str(path)))
    return deps


PARSERS = {
    "requirements.txt": parse_requirements_txt,
    "pyproject.toml": parse_pyproject_toml,
    "package.json": parse_package_json,
}


# Directories never worth descending into when searching for manifests: version
# control internals, and anything the corresponding package manager itself
# populates with *installed* (not declared) dependencies. `node_modules` in
# particular can contain thousands of nested `package.json` files belonging to
# already-installed third-party packages, not the project's own declared
# dependencies — recursing into it would re-check the entire resolved
# dependency graph (slow, and every name in it is by definition already on the
# registry, so it's also pure noise). Dot-directories (`.git`, `.venv`, a
# pip/uv virtualenv, `.tox`, editor/CI config dirs) are skipped as a group for
# the same reason: none of them hold manifests a human wrote, and `.venv`
# specifically can contain an installed package's own `pyproject.toml`.
_SKIP_DIR_NAMES = {"node_modules"}


def find_manifests(root: Path) -> list[Path]:
    """Find every manifest file under `root`, recursing into subdirectories.

    A real monorepo (pnpm/Yarn/npm workspaces, Turborepo, Nx, Lerna, a Python
    src-layout with multiple packages) declares its actual dependencies across
    many nested manifests, not just one at the root — e.g. `vitejs/vite`'s
    root `package.json` has zero runtime `dependencies` at all; the published
    package's real runtime deps (`lightningcss`, `rolldown`, etc.) live in
    `packages/vite/package.json`. A non-recursive scan of the root directory
    alone silently misses every one of them, which defeats the entire point
    of a supply-chain check on a monorepo. `node_modules` and dot-directories
    are pruned during the walk (not just filtered from the result) so the
    walk itself never descends into them.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for filename in PARSERS:
            if filename in filenames:
                found.append(Path(dirpath) / filename)
    return found


def parse_manifest(path: Path) -> list[Dependency]:
    parser = PARSERS.get(path.name)
    if parser is None and path.suffix == ".txt":
        # `find_manifests` only auto-discovers the exact name "requirements.txt",
        # but a real pip requirements file is routinely named something else —
        # pip itself doesn't care about the filename at all, only real-world
        # convention does. Home Assistant's core repo splits into
        # requirements_test.txt/requirements_test_pre_commit.txt; cookiecutter-
        # django splits into requirements/base.txt, local.txt, production.txt.
        # A user pointing slopcheck directly at one of these (a natural thing
        # to do, and exactly how the new `-r`-recursion above resolves nested
        # files too) used to hit `PARSERS[path.name]` -> KeyError, an unhandled
        # traceback instead of a clean error or a real scan. Any other `.txt`
        # file is a reasonable enough match for the line-based requirements
        # format (there's no other `.txt`-suffixed manifest this tool knows
        # about) to treat the same way rather than crash.
        parser = parse_requirements_txt
    if parser is None:
        raise ManifestParseError(
            f"{path}: don't know how to parse this file "
            "(expected requirements.txt, pyproject.toml, package.json, or a *.txt requirements file)"
        )
    try:
        return parser(path)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ManifestParseError(f"{path}: couldn't parse as {path.name} ({e})") from e
