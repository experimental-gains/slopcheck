"""Extract dependency names from common manifest files.

Only the package name is needed (not version constraints) since the
whole point is checking whether the name exists in a registry at all.
"""
from __future__ import annotations

import configparser
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
    deps, _touched = _parse_requirements_txt(path, set())
    return deps


def requirements_txt_files_touched(path: Path) -> set[Path]:
    """Every requirements-format file `path` pulls in via `-r`/`--requirement`, plus itself.

    Needed to detect a `-i`/`--extra-index-url` directive: pip applies one
    found in *any* file reached this way to the whole install (confirmed
    live), not just the literal file named on the command line. A
    directive-only file (e.g. a shared `base.txt` that just sets the index
    and is pulled in by every per-environment file via `-r`, with no
    dependency lines of its own) never shows up as any `Dependency`'s
    `source` — so the caller needs this file set directly rather than
    inferring it from already-parsed dependencies.
    """
    _deps, touched = _parse_requirements_txt(path, set())
    return touched


def _parse_requirements_txt(path: Path, seen: set[Path]) -> tuple[list[Dependency], set[Path]]:
    resolved = path.resolve()
    if resolved in seen:
        # A `-r` cycle (directly or indirectly self-referencing). Real pip
        # would also loop forever on this, but there's no reason to hang or
        # crash a name-existence check over a malformed requirements file —
        # just stop recursing into an already-visited file.
        return [], seen
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
            nested_deps, seen = _parse_requirements_txt(target, seen)
            deps.extend(nested_deps)
            continue
        if _CONSTRAINT_FILE_RE.match(line) or line.startswith(("-e ", "--")):
            continue
        if "://" in line:
            continue
        match = _REQ_LINE_RE.match(line)
        if match:
            deps.append(Dependency(match.group("name"), "pypi", str(path)))
    return deps, seen


def _pep621_deps(data: dict) -> list[str]:
    names = []
    project = data.get("project", {})
    for spec in project.get("dependencies", []):
        names.append(spec)
    for group in project.get("optional-dependencies", {}).values():
        names.extend(group)
    return names


def _build_system_deps(data: dict) -> list[str]:
    """PEP 518's `[build-system] requires` — the packages pip installs into an isolated build env.

    [PEP 518](https://peps.python.org/pep-0518/) makes this table mandatory
    for any project pip can build from source, and it's a plain list of PEP
    508 requirement strings (the same syntax as `[project.dependencies]`)
    naming real PyPI packages pip installs *before* running the build at
    all — e.g. `numpy`'s actual, current `pyproject.toml`: `[build-system]
    requires = ["meson-python>=0.20.0", "Cython>=3.1.0"]`. Neither of those
    names appears anywhere under `[project]`, so `_pep621_deps` (which only
    reads `[project.dependencies]`/`[project.optional-dependencies]`) never
    saw them, and this table had no reader at all anywhere in this module —
    confirmed live: a `pyproject.toml` with a hallucinated name planted only
    in `[build-system] requires` (alongside a real `[project.dependencies]`
    entry that *was* checked) produced a clean report that checked just the
    one real dependency and said nothing about the fabricated build
    backend, a silent false negative on a field every modern
    pyproject.toml-based project ships. Build requirements don't support
    extras/markers referencing the project's own name (self-referential
    extras are a `[project.optional-dependencies]`/`[dependency-groups]`
    concept, not a build-system one) and don't participate in
    `[tool.uv.sources]`/Poetry table-form git/path/url overrides — those are
    scoped to regular dependency resolution, not the separate PEP 517
    build-frontend install step — so, unlike `_pep621_deps`, nothing here
    needs to be cross-referenced against `skip_names`.
    """
    build_system = data.get("build-system", {})
    return list(build_system.get("requires", []))


def _hatch_deps(data: dict) -> list[str]:
    """Flatten Hatch's `[tool.hatch.env].requires` and per-environment `dependencies` into requirement specs.

    Hatch (https://hatch.pypa.io/latest/config/environment/overview/,
    the PyPA-recommended build backend/env manager) has two distinct
    tables of its own, neither PEP 621 nor PEP 735 and neither read by any
    function above: `[tool.hatch.env] requires` lists environment-plugin
    packages Hatch itself installs (via its own resolver, not even pip)
    before it can even parse the rest of an environment's config, and each
    `[tool.hatch.envs.<name>] dependencies` lists plain PEP 508 requirement
    strings installed into that named environment. Confirmed live with
    Hatch 1.18.1 against a scratch project: a `[tool.hatch.envs.default]
    dependencies = ["totally-hallucinated-package-xyz-123"]` table made
    `hatch env create` genuinely fail resolving the fake name from PyPI
    ("Could not find a version that satisfies the requirement ... (from
    versions: none)"), and a separate `[tool.hatch.env] requires =
    ["totally-hallucinated-hatch-plugin-xyz-987"]` made the same command
    fail even earlier, while syncing environment plugin requirements
    ("No solution found when resolving dependencies ... was not found in
    the package registry"). Before this fix neither table had any reader
    here at all, so a hallucinated name planted in either one — a real
    place for one to land, since Hatch is the build backend this project's
    own dependents are as likely to use as Poetry or PDM — sailed through
    unchecked even though a real `hatch env create` genuinely tries to
    install it. Unlike Poetry's/uv's dependency tables, Hatch's own docs
    only document these two fields as plain requirement-string lists (no
    git/path/url table form), so no registry-source filtering is needed
    here the way `_is_poetry_registry_dep`/`_is_uv_registry_source` do it
    for their own tables.
    """
    hatch = data.get("tool", {}).get("hatch", {})
    names = list(hatch.get("env", {}).get("requires", []))
    for env in hatch.get("envs", {}).values():
        if isinstance(env, dict):
            names.extend(env.get("dependencies", []))
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


def _pdm_dev_deps(data: dict) -> list[str]:
    """Flatten PDM's legacy `[tool.pdm.dev-dependencies]` groups into requirement specs.

    PDM (https://pdm-project.org/en/latest/usage/dependency/#add-development-only-dependencies,
    "Added in 1.5.0") predates PEP 735 and originally shipped its own table for
    dev-only dependency groups, structurally identical to `[dependency-groups]`
    (a table of group-name -> list of PEP 508 requirement strings) but under
    `[tool.pdm.dev-dependencies]` instead of the later standardized top-level
    table `_dependency_groups_deps` already reads. PDM's own current docs steer
    `pdm add -dG <group>` toward writing `[dependency-groups]` now, but this
    older table is not deprecated or ignored — confirmed live (PDM 2.29.2,
    `pdm lock -v` against a `pyproject.toml` with a `[tool.pdm.dev-dependencies]
    test = ["totally-hallucinated-package-xyz-123"]` table and nothing
    referencing that name anywhere else): PDM genuinely read the table and
    tried to resolve the fake name from PyPI (`CandidateNotFound: Unable to
    find candidates for totally-hallucinated-package-xyz-123`), i.e. a real
    `pdm install`/`pdm lock` installs whatever is planted here just as much as
    a `[dependency-groups]` entry. Before this fix, nothing in this module read
    `[tool.pdm.dev-dependencies]` at all, so any project still using this
    still-current, still-honored legacy form (a real, common state for any
    PDM project created before the `[dependency-groups]` migration, or one
    that simply hasn't re-run `pdm add -dG` since) had every name in it
    silently never checked — the same silent false-all-clear shape as the
    other legacy-but-still-real dependency tables already fixed here
    (`Pipfile`, `setup.cfg`).

    A group entry can also be a PDM-added editable/local/URL/VCS dependency —
    confirmed live via `pdm add -e ./sub-package --dev`, which wrote
    `"-e file:///${PROJECT_ROOT}/sub-package#egg=subpkg"` into this same
    table (PDM always writes these as a `pip`-style requirement *string*,
    never a table, even for local paths and VCS URLs — confirmed live and
    against PDM's own docs, unlike Poetry's/uv's dict-shaped git/path/url
    overrides). Such a string needs no special-casing to exclude: it starts
    with `-e ` or contains `://`, neither of which `_REQ_LINE_RE` matches
    (it requires a leading name character), so it's already dropped the same
    way `parse_pyproject_toml`'s main loop drops any other spec the regex
    doesn't match, exactly mirroring how `_parse_requirements_txt` treats an
    `-e`/URL line as non-registry-resolvable rather than a package name.
    """
    names = []
    for group in data.get("tool", {}).get("pdm", {}).get("dev-dependencies", {}).values():
        for item in group:
            if isinstance(item, str):
                names.append(item)
    return names


def _setuptools_dynamic_files(spec) -> list[str]:
    if not isinstance(spec, dict):
        return []
    file_value = spec.get("file")
    if file_value is None:
        return []
    return [file_value] if isinstance(file_value, str) else list(file_value)


def _setuptools_dynamic_deps(data: dict, base_dir: Path) -> list[Dependency]:
    """Resolve PEP 621 `dynamic` dependencies setuptools loads from a file.

    setuptools (https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html#dynamic-metadata)
    lets `[project].dynamic` list "dependencies"/"optional-dependencies" and defer
    their actual values to `[tool.setuptools.dynamic]`, which points at one or more
    requirements-style files instead of listing specs inline. Per PEP 621 itself, a
    field listed as dynamic must *not* also appear directly under `[project]`, so
    `_pep621_deps` (which only reads `[project.dependencies]`/
    `[project.optional-dependencies]`) silently sees nothing at all for a file using
    this feature. Confirmed real and current, not a hypothetical: compas-dev/compas's
    actual pyproject.toml (`dynamic = ['dependencies', 'optional-dependencies',
    'version']`, `[tool.setuptools.dynamic] dependencies = { file = "requirements.txt" }`)
    — the pre-fix parser reported "0 dependencies checked" against it despite 5 real
    runtime deps (jsonschema, networkx, numpy, scipy, watchdog) and 11 dev deps in
    requirements.txt/requirements-dev.txt, none of them ever checked. File paths are
    resolved relative to the directory containing pyproject.toml (setuptools' own
    documented behavior); each referenced file is read the same way as a standalone
    requirements.txt, since that's the format setuptools itself expects here.
    """
    dynamic_fields = set(data.get("project", {}).get("dynamic", []))
    setuptools_dynamic = data.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    deps: list[Dependency] = []

    if "dependencies" in dynamic_fields:
        for filename in _setuptools_dynamic_files(setuptools_dynamic.get("dependencies")):
            deps.extend(parse_requirements_txt(base_dir / filename))

    if "optional-dependencies" in dynamic_fields:
        for group_spec in setuptools_dynamic.get("optional-dependencies", {}).values():
            for filename in _setuptools_dynamic_files(group_spec):
                deps.extend(parse_requirements_txt(base_dir / filename))

    return deps


def _is_pipfile_registry_dep(spec) -> bool:
    """Whether a Pipfile `[packages]`/`[dev-packages]` entry resolves via a package index.

    Pipenv's table form (https://pipenv.pypa.io/en/latest/specifiers/#specifying-versions-of-a-package)
    lets an entry point at a git remote, a local path, or a local file/sdist
    instead of PyPI — the same non-registry-source situation as Poetry's
    git/path/url table form (`_is_poetry_registry_dep` above) and uv's
    `[tool.uv.sources]` (`_is_uv_registry_source` above). A plain string
    version constraint — including the bare `"*"` Pipenv always writes for
    an unpinned `pipenv install <name>` — still resolves against PyPI and
    should be checked; only a table with a `git`/`path`/`file` key opts out.
    """
    if isinstance(spec, dict):
        return not any(key in spec for key in ("git", "path", "file"))
    return True


def _pipfile_deps(data: dict, section: str) -> list[str]:
    names = []
    for name, spec in data.get(section, {}).items():
        if not _is_pipfile_registry_dep(spec):
            continue
        names.append(name)
    return names


def parse_pipfile(path: Path) -> list[Dependency]:
    """Parse Pipenv's `Pipfile` (not `Pipfile.lock`).

    Pipenv is still a widely-used dependency manager (Home Assistant's own
    `pipenv`-based dev requirements, plenty of Django/Flask tutorials and
    real projects) with a manifest format `find_manifests`/`PARSERS` never
    recognized at all — unlike `requirements.txt`/`pyproject.toml`/
    `package.json`, `Pipfile` has no filename match anywhere, so a
    Pipenv-only project scanned nothing and either hit the "no manifest
    found" error (if `Pipfile` were literally the only file in the
    directory) or, far more dangerously and silently, reported "0
    dependencies checked, all clean" whenever any other supported-but-
    dependency-free manifest happened to sit alongside it — e.g. a
    `pyproject.toml` that exists purely for `[tool.ruff]`/`[tool.black]`
    config, a real, common combination in Pipenv-managed repos that keep
    tool config in `pyproject.toml` while pinning actual dependencies in
    `Pipfile`. Confirmed live: a scratch project with exactly that
    combination — a `Pipfile` naming real (`requests`) and clearly
    hallucinated-style packages plus a config-only `pyproject.toml` — was
    reported as "0 dependencies checked, all clean" with exit code 0
    before this fix, a false all-clear despite the dependencies sitting
    right there unread.

    `Pipfile` is TOML (unlike its companion `Pipfile.lock`, which is JSON),
    so it's parsed the same way as `pyproject.toml`. Only `[packages]` and
    `[dev-packages]` are read; `[requires]` (Python version) and `[[source]]`
    (Pipenv's own private-index config, analogous to Poetry's
    `[[tool.poetry.source]]`) aren't dependency names.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    names = _pipfile_deps(data, "packages") + _pipfile_deps(data, "dev-packages")
    return [Dependency(name, "pypi", str(path)) for name in names]


def _setup_cfg_list_deps(value: str, path: Path) -> list[Dependency]:
    """Split a setup.cfg list-valued option the same way setuptools itself does.

    setuptools' own list parser (`ConfigHandler._parse_list`, shared by
    `install_requires` via `_parse_requirements_list`, `setup_requires`, and
    every `[options.extras_require]` value — confirmed by reading
    `setuptools/config/setupcfg.py` from setuptools 84.0.0, this project's
    own pinned minimum) makes an either/or choice at the whole-value level:
    if the raw config value contains a newline at all, it splits on lines;
    otherwise it splits on `;`. A single physical line like `install_requires
    = requests;totally-hallucinated-package-xyz-123` is real, current syntax
    setuptools genuinely expands into two separate requirements — confirmed
    live: `setuptools.config.setupcfg.read_configuration` on exactly that
    value returns `['requests', 'totally-hallucinated-package-xyz-123']`.

    This function used to always split on newlines only (`value.splitlines()`),
    regardless of whether the value contained any `;`-separated entries at
    all. For a single-line semicolon list, that treats the whole string as
    one line, which `_REQ_LINE_RE` then matches as a single requirement:
    the first name before the first `;` becomes the extracted package, and
    everything after it (including any further `;`-separated names) is
    swallowed by the regex's own trailing `[<>=!~;].*` alternative — the
    same shape that legitimately matches a real PEP 508 environment marker
    like `; python_version < "3.8"`. A hallucinated package placed second
    or later in a single-line semicolon list was silently never even seen,
    let alone checked against PyPI — a real false negative, not a cosmetic
    style difference, since a real `pip install`/`python setup.py install`
    resolves it exactly the same as a name on its own line.
    """
    chunks = value.splitlines() if "\n" in value else value.split(";")
    deps = []
    for raw_chunk in chunks:
        line = raw_chunk.strip()
        if not line:
            continue
        line = _strip_inline_comment(line)
        if not line or "://" in line:
            continue
        match = _REQ_LINE_RE.match(line)
        if match:
            deps.append(Dependency(match.group("name"), "pypi", str(path)))
    return deps


def _setup_cfg_requirements_value(value: str, path: Path) -> list[Dependency]:
    """Resolve an `install_requires`/`[options.extras_require]` value, following
    setuptools' own `file:` directive when present.

    setuptools' real parser for these two fields specifically (confirmed live
    by reading setuptools 84.0.0's `setupcfg.py`: `'install_requires':
    partial(self._parse_requirements_list, ...)` and the same
    `_parse_requirements_list` used for every `[options.extras_require]`
    value) doesn't hand the raw config value straight to the semicolon-
    or-newline splitter this module already has in `_setup_cfg_list_deps`.
    It first calls `_parse_file_in_root`, which checks whether the value
    starts with the literal `file:` — and if so, treats everything after it
    as a comma-separated list of paths *relative to the directory containing
    setup.cfg*, reads each one, and joins their contents with "\\n" — *then*
    runs that concatenated text through the same splitter. So
    `install_requires = file:requirements.txt` is real, current syntax that
    resolves to the actual contents of `requirements.txt`, not a literal
    string to check against PyPI.

    Confirmed real and current, not a hypothetical: `CleanCut/green` (a real,
    actively maintained PyPI package — `pip install green`) declares its
    dependencies exactly this way in its shipped `setup.cfg`:
    `install_requires = file:requirements.txt` and
    `[options.extras_require] dev = file:requirements-dev.txt`, both
    resolving (confirmed against setuptools 84.0.0's own
    `read_configuration`) to real requirement lists (colorama, coverage,
    lxml, setuptools, unidecode; black, coverage[toml], django, mypy,
    testtools). Before this fix, `_setup_cfg_list_deps` received the literal
    string `"file:requirements.txt"` unchanged — `_REQ_LINE_RE` doesn't match
    it (`:` isn't a valid name or version-specifier character), so it was
    silently dropped and the entire referenced file's dependencies were never
    checked: the same silent "0 dependencies checked, all clean"
    false-all-clear shape as the Pipfile/`[build-system] requires`/PEP 621
    `dynamic` gaps already fixed here. Reuses `parse_requirements_txt` (not
    the simpler `_setup_cfg_list_deps`) for the referenced file the same way
    `_setuptools_dynamic_deps` already does for pyproject.toml's own
    `file`-sourced dynamic dependencies, so `Dependency.source` correctly
    points at the file the name was actually found in.

    `[options] setup_requires` deliberately does NOT get this treatment: its
    parser entry is the plain `self._parse_list_semicolon`, not
    `_parse_requirements_list` — setuptools itself never expands a `file:`
    directive there.
    """
    stripped = value.strip()
    if not stripped.startswith("file:"):
        return _setup_cfg_list_deps(value, path)

    base_dir = path.parent
    deps: list[Dependency] = []
    for raw_filename in stripped[len("file:") :].split(","):
        filename = raw_filename.strip()
        if filename:
            deps.extend(parse_requirements_txt(base_dir / filename))
    return deps


def parse_setup_cfg(path: Path) -> list[Dependency]:
    """Parse setuptools' legacy `setup.cfg` `[options]`/`[options.extras_require]`.

    Plenty of real, currently-maintained packages still declare dependencies
    this way instead of PEP 621's `[project.dependencies]` — confirmed via a
    live GitHub code search (25k+ hits for `install_requires` in `setup.cfg`)
    and two concrete current examples: `RDFLib/sparqlwrapper` and
    `rm-hull/luma.oled`. Both matter more than an ordinary missing-format
    gap because of *what else* is in their repos: PEP 518 requires any
    project pip can build from source to ship a `pyproject.toml` with a
    `[build-system]` table, so both repos have one — but neither has
    migrated its actual dependency list to `[project.dependencies]`, so
    their `pyproject.toml` has no `[project]` table at all. Before this fix,
    `find_manifests` matched that `pyproject.toml` (a real, supported
    filename), `parse_pyproject_toml` correctly found zero PEP 621/Poetry/
    uv/dependency-groups deps in it (there are none), and `setup.cfg` itself
    had no `PARSERS` entry — so the *only* manifest found for either project
    was one that legitimately contains no dependencies. Same silent
    "0 dependencies checked, all clean" false-all-clear failure mode as the
    `Pipfile` gap this tool already fixed, not a loud "no manifest found"
    error that would at least surface the gap.

    Uses `configparser` (the same library setuptools itself parses this
    format with) rather than hand-rolled line splitting, with interpolation
    turned off: setup.cfg's `[options.extras_require]` supports
    `%(other_extra)s` back-references between extras, and resolving that
    correctly would mean re-implementing configparser's own interpolation;
    turning it off just means a `%(...)s` reference doesn't match
    `_REQ_LINE_RE` (starts with `%`, not a name character) and is skipped
    like any other unresolvable spec, rather than either resolving nothing
    at all or raising on a reference this simple parser doesn't need to
    understand.

    `[options] setup_requires` names packages pip/setuptools installs into
    the build environment *before* running `setup.py` at all — the setup.cfg
    analog of PEP 518's `[build-system] requires` (`_build_system_deps`
    above), for the same legacy, still-real setup.cfg-based projects this
    function already exists to support. Confirmed current, not a stale
    relic: setuptools 84.0.0 (the exact minimum this project's own
    `[build-system] requires` pins) still carries a live parser for it
    (`setupcfg.py`'s `ConfigOptionsHandler.parsers`: `'setup_requires':
    self._parse_list_semicolon`), and pyscaffold's own real, current
    `setup.cfg` uses exactly this field (`[options] setup_requires =
    pyscaffold>=3.2a0,<3.3a0`). Confirmed live against this parser before
    the fix: a `setup.cfg` with a real `install_requires` entry alongside a
    hallucinated name planted only in `setup_requires` produced a clean
    "1 dependency checked, all clean" report that silently said nothing
    about the fabricated build-time dependency — the same silent
    false-all-clear shape as the `Pipfile`/`[build-system] requires` gaps
    already fixed here, not a loud parse error that would at least surface
    the gap. Same multi-line-or-semicolon-separated list shape as
    `install_requires` (setuptools' own `_parse_list_semicolon` falls back
    to newline-splitting whenever the value contains a `\n`, which every
    real multi-line setup.cfg list does), so it reuses
    `_setup_cfg_list_deps` unchanged.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(path.read_text(encoding="utf-8-sig"), source=str(path))

    deps = []
    if parser.has_option("options", "install_requires"):
        deps.extend(_setup_cfg_requirements_value(parser.get("options", "install_requires"), path))
    if parser.has_option("options", "setup_requires"):
        deps.extend(_setup_cfg_list_deps(parser.get("options", "setup_requires"), path))
    if parser.has_section("options.extras_require"):
        for value in parser["options.extras_require"].values():
            deps.extend(_setup_cfg_requirements_value(value, path))
    return deps


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


def _self_referential_name(data: dict) -> set[str]:
    """The project's own PEP 503-normalized name, if declared, as a skip-set.

    PEP 621 explicitly supports a "self-referential extra" — an entry in
    `[project.optional-dependencies]` (or, just as commonly in practice, in
    PEP 735 `[dependency-groups]`) that names the *current* project with a
    different combination of its own extras, e.g. `all = ["your-project-
    name[gui, cli]"]`, so an "all"/"test"-style umbrella extra doesn't need
    its own hand-maintained copy of every other extra's dependency list
    (https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#self-referential-extras,
    "most package managers now support this kind of extra, including pip,
    uv, ..."). Confirmed real and current, not hypothetical: PDM's own
    `pyproject.toml` does exactly this — `[project.optional-dependencies]
    template = ["pdm[copier,cookiecutter]"]` / `all = ["pdm[keyring,
    template]"]`, and `[dependency-groups] test = ["pdm[pytest]", ...]` —
    naming `pdm` itself, not an external dependency. Before this fix,
    `_REQ_LINE_RE` matched that self-reference like any other spec and
    added `pdm` as a PyPI dependency to check, which happens to be
    harmless for an already-published project like PDM but is a real
    false positive waiting to happen for the much more common case this
    pattern is aimed at: a brand-new, not-yet-published project using its
    own `all`/`everything` extra during early development, where checking
    its own name against PyPI can only ever produce a spurious "not found"
    or "recent" flag on a name that was never an external dependency (and
    isn't a slopsquatting risk) in the first place. Matched PEP 503-
    normalized (case- and separator-insensitive), the same rule used
    throughout this module for name equality, since `[project.name]` is
    itself normalized that way.
    """
    name = data.get("project", {}).get("name")
    return {_normalize_name(name)} if isinstance(name, str) else set()


def parse_pyproject_toml(path: Path) -> list[Dependency]:
    # skip_names (derived from [tool.uv.sources]'s git/path/workspace/url
    # entries and the project's own self-referential-extra name) only means
    # something for the *regular* dependency resolver — uv/pip reading
    # [project.dependencies]/[project.optional-dependencies]/
    # [dependency-groups]/[tool.pdm.dev-dependencies]/[tool.poetry.*], the
    # one thing [tool.uv.sources] actually overrides. [build-system]
    # requires and Hatch's [tool.hatch.env]/[tool.hatch.envs.*] tables are
    # resolved by two completely separate mechanisms that never consult
    # [tool.uv.sources] at all: a PEP 517 isolated build environment (plain
    # pip, with no notion of uv-specific config) and Hatch's own env
    # manager. Confirmed live (venv + pip 25.x, this box): a pyproject.toml
    # with `[tool.uv.sources] totally-hallucinated-buildreq-xyz-123 = {
    # path = "./local-pkg" }` and `[build-system] requires = ["setuptools",
    # "totally-hallucinated-buildreq-xyz-123"]` made `pip install .`
    # genuinely try to fetch that name from PyPI while installing build
    # dependencies and fail ("Could not find a version that satisfies the
    # requirement ... (from versions: none)") — pip's build-isolation step
    # never parses [tool.uv.sources]. Before this fix, both tables' output
    # was folded into the same raw_specs list as the project-level tables
    # and filtered through the same skip_names set, so a name that merely
    # happened to collide with an unrelated [tool.uv.sources] entry (or the
    # project's own name) got silently skipped here too — the same false
    # negative _build_system_deps's/_hatch_deps's own docstrings already
    # describe (neither participates in [tool.uv.sources] overrides), just
    # not actually enforced by this function until now.
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    raw_specs = (
        _pep621_deps(data)
        + _poetry_deps(data)
        + _dependency_groups_deps(data)
        + _pdm_dev_deps(data)
    )
    skip_names = _uv_non_registry_names(data) | _self_referential_name(data)
    deps = []
    for spec in raw_specs:
        match = _REQ_LINE_RE.match(spec.strip())
        if match:
            name = match.group("name")
            if _normalize_name(name) in skip_names:
                continue
            deps.append(Dependency(name, "pypi", str(path)))
    for spec in _build_system_deps(data) + _hatch_deps(data):
        match = _REQ_LINE_RE.match(spec.strip())
        if match:
            deps.append(Dependency(match.group("name"), "pypi", str(path)))
    for dep in _setuptools_dynamic_deps(data, path.parent):
        if _normalize_name(dep.name) in skip_names:
            continue
        deps.append(dep)
    return deps


# Version protocols that point at something other than the public registry
# (a workspace sibling, a local path, a git remote). Names under these
# protocols are never expected to resolve on npm even when legitimate —
# monorepo tooling (pnpm/Yarn/npm workspaces, Turborepo, Nx, Lerna) commonly
# names internal-only packages this way, so checking them against the
# registry produces a false "not found" on every workspace monorepo.
_NON_REGISTRY_PREFIXES = ("workspace:", "file:", "link:", "portal:", "git:", "git+", "github:")

_NPM_ALIAS_PREFIX = "npm:"

_NPM_PATCH_PREFIX = "patch:"


def _npm_looks_like_hosted_git_or_path(version: str) -> bool:
    """Whether an npm dependency's version value is a git-host shorthand or local path, not a registry range.

    npm's own dependency resolver (npm-package-arg's `resolve()`, read
    directly from `/usr/share/nodejs/npm-package-arg/lib/npa.js` on this box)
    checks the version value against `HostedGit.fromUrl()` (recognizes
    `github:`/`gitlab:`/`bitbucket:`/`gist:` shorthand, *and* the bare
    `"user/repo"` form with no protocol at all, which defaults to GitHub)
    first; if that doesn't match and the value isn't a URL, it falls back to
    a blanket `hasSlashes.test(spec)` check and, if that matches, resolves it
    as a local file/directory path instead of a registry spec — never the
    public registry either way. Confirmed live (npm 9.2.0, `npm install
    --dry-run`, a fake name under a version value with no other recognized
    protocol): `"totally-hallucinated-name-xyz-987": "sindresorhus/is-odd"`
    made npm run `git ls-remote ssh://git@github.com/sindresorhus/is-odd.git`
    and never contact the npm registry for that name at all — the exact
    silent-false-positive shape `_NON_REGISTRY_PREFIXES` already exists to
    avoid for `github:user/repo`, just missing the un-prefixed shorthand
    form and the `gitlab:`/`bitbucket:` prefixes (also confirmed live the
    same way: both genuinely dispatch to `git ls-remote` against their own
    host, `gitlab.com`/`bitbucket.org`, regardless of whether the named repo
    exists). A single `"/" in version` check subsumes all of these at once,
    matching npm's own fallback exactly, since no valid registry version
    range, exact version, or dist-tag can ever contain a literal `/` (a
    dist-tag containing one fails npm's own `encodeURIComponent` equality
    check before it would ever reach the registry). Applied only to the
    plain `dependencies`/`devDependencies`/`peerDependencies`/
    `optionalDependencies` sections below: `overrides`/`resolutions` entries
    already take the *key*, not this value, as the real registry name to
    check (the key is what's being overridden, still a real package name
    regardless of what non-registry source its replacement comes from), so
    they're unaffected by this.

    Deliberately does NOT match Yarn Berry's own `patch:<name>@<descriptor>#
    <path>` protocol (applying a local patch file on top of an otherwise
    normal dependency), even though a real one always contains a `/` (the
    patch file path) and would otherwise trip this same fallback — found via
    real-world testing against babel/babel's actual `package.json`, which
    has exactly this shape for two real devDependencies patched with `yarn
    patch`: `"@rollup/plugin-commonjs": "patch:@rollup/plugin-
    commonjs@npm%3A29.0.2#~/.yarn/patches/....patch"`. Unlike the git-host/
    file-path forms above, the wrapped descriptor here is still a real
    registry reference (`npm%3A29.0.2` is `npm:29.0.2`, URL-encoded) — Yarn
    resolves the *named key* from the registry first and only then applies
    the patch on top, so the key remains exactly as checkable as an
    ordinary dependency. Confirmed live (Yarn Berry 4.5.0, `yarn install`
    against a scratch `patch:totally-hallucinated-name-xyz-987@npm%3A1.0.0#
    ...` entry): Yarn genuinely queried `https://registry.yarnpkg.com/
    totally-hallucinated-name-xyz-987` (a real npm registry mirror) and got
    a real 404 for the fake name, rather than skipping registry resolution
    the way the git-host-shorthand/local-path forms above do. Before this
    carve-out, treating every `patch:`-prefixed value as non-registry (the
    same blanket "any slash" rule applied to everything else) would have
    silently dropped both of babel/babel's real patched dependencies from
    checking — trading the git-shorthand false positive this function
    exists to fix for a new false negative on an equally real, current
    Yarn Berry pattern.
    """
    if version.startswith(_NPM_PATCH_PREFIX):
        return False
    return "/" in version


def _npm_non_registry_version(version: str) -> bool:
    """Whether an npm dependency's version value should be skipped rather than checked against the registry.

    Combines the explicit `_NON_REGISTRY_PREFIXES` match (`workspace:`,
    `file:`, etc. — protocols that can show up with no `/` in them at all,
    e.g. `"workspace:*"`) with the broader git-host-shorthand-or-path
    fallback above (protocols/forms that always contain a `/`, so checking
    a prefix for them separately would be redundant, not just repetitive —
    `ruff`'s SIM114 flags exactly that redundancy when the two checks are
    left as sibling `elif` branches instead of merged here).
    """
    return version.startswith(_NON_REGISTRY_PREFIXES) or _npm_looks_like_hosted_git_or_path(version)


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


def _npm_workspace_patterns(data: dict) -> list[str]:
    """Extract glob patterns from a package.json's `workspaces` field.

    npm (native workspaces, npm 7+) and Yarn Classic (v1) both accept either
    the plain array form (`"workspaces": ["packages/*"]`) or Yarn Classic's
    object form (`"workspaces": {"packages": [...], "nohoist": [...]}`) --
    `nohoist` is a hoisting hint, not a member-location pattern, so only
    `packages` is read from the object form.
    """
    workspaces = data.get("workspaces")
    if isinstance(workspaces, list):
        return [p for p in workspaces if isinstance(p, str)]
    if isinstance(workspaces, dict):
        return [p for p in workspaces.get("packages", []) if isinstance(p, str)]
    return []


def npm_workspace_member_names(package_json_paths: list[Path]) -> list[tuple[Path, set[str]]]:
    """For every package.json declaring `workspaces`, the local package names it resolves without the registry.

    npm (native workspaces, npm 7+) and Yarn Classic (v1) both let a
    package.json list one or more sibling packages under `workspaces` (a
    glob pattern like `"packages/*"`) and then depend on a matching sibling
    by its ordinary declared name with a plain semver range or `"*"` --
    *not* pnpm's/Yarn Berry's explicit `workspace:` protocol prefix, which
    `_NON_REGISTRY_PREFIXES` above already skips. A real `npm install`/
    `yarn install` resolves that name entirely locally (a symlink into the
    workspace directory) and never queries the public registry for it at
    all. Confirmed live (npm 9.2.0 and Yarn Classic 1.22.22, a from-scratch
    two-package workspace: root `{"workspaces": ["packages/*"], "devDependencies":
    {"@scratch/internal-lib": "^1.0.0"}}`, member `packages/internal-lib/
    package.json` = `{"name": "@scratch/internal-lib", "version": "1.0.0",
    "private": true}`): `npm install --loglevel silly` traced
    `placeDep ROOT @scratch/internal-lib@1.0.0 ... want: file:.../packages/
    internal-lib` and made zero `registry.npmjs.org` requests for that name
    (the only registry hit was an unrelated bulk security-advisory POST),
    and `yarn install --verbose` logged `Creating symlink ... to
    ".../packages/internal-lib"` with no `registry.yarnpkg.com` request
    either -- and both still resolved it purely locally even when the
    declared range didn't actually satisfy the local package's version
    (`^2.0.0` against a local `1.0.0`), so no version-compatibility check is
    needed here, just a name match.

    Real and current, not a hypothetical: npm's own monorepo (npm/cli)
    dogfoods exactly this pattern -- its root `package.json` lists
    `"@npmcli/docs": "^1.0.0"`, `"@npmcli/mock-registry": "^1.0.0"`, and
    `"@npmcli/mock-globals": "^1.0.0"` in `devDependencies` as plain semver
    ranges with no `workspace:` prefix, and each of those packages' own
    package.json is `"private": true` -- confirmed live, all three names
    genuinely 404 on `registry.npmjs.org` (never published, intentionally).
    Before this fix, scanning that repo (or any real npm/Yarn-Classic-
    workspaces monorepo shaped this way -- the *default* layout for Lerna,
    Nx, and Turborepo projects, not an obscure one) reported every private,
    unpublished workspace member as a plain `not_found` "hallucinated"
    dependency: a false positive on the single most common JS monorepo
    structure.

    Returns a list of (workspace root directory, member names) pairs rather
    than one flat set, so a caller can scope the skip to dependents actually
    inside that workspace's own directory subtree -- avoiding a false
    negative if an unrelated project happens to be scanned in the same run
    and coincidentally declares a same-named but genuinely *external*
    dependency that was never meant to resolve locally.
    """
    pairs: list[tuple[Path, set[str]]] = []
    for path in package_json_paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        patterns = _npm_workspace_patterns(data)
        if not patterns:
            continue
        root = path.parent
        names: set[str] = set()
        for pattern in patterns:
            # A leading "!" negates a pattern (excludes matches an earlier,
            # broader pattern already matched) -- not modeled here; treating
            # a negated pattern's matches the same as any other only risks
            # the same safe over-approximation this module already accepts
            # elsewhere (e.g. `uv_private_registry_context`'s docstring:
            # wrongly skipping a name beats wrongly flagging a real local
            # package as a hallucination).
            glob_pattern = pattern[1:] if pattern.startswith("!") else pattern
            try:
                matches = list(root.glob(glob_pattern))
            except (ValueError, NotImplementedError):
                continue
            for member_dir in matches:
                member_pkg = member_dir / "package.json"
                if not member_pkg.is_file():
                    continue
                try:
                    member_data = json.loads(member_pkg.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                name = member_data.get("name")
                if isinstance(name, str):
                    names.add(name)
        if names:
            pairs.append((root, names))
    return pairs


def parse_package_json(path: Path) -> list[Dependency]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    deps = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, version in data.get(section, {}).items():
            if isinstance(version, str) and version.startswith(_NPM_ALIAS_PREFIX):
                name = _npm_alias_target(version)
            elif isinstance(version, str) and _npm_non_registry_version(version):
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
    "Pipfile": parse_pipfile,
    "setup.cfg": parse_setup_cfg,
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
#
# A virtualenv isn't always dot-prefixed, though: Python's own `venv` module
# docs (https://docs.python.org/3/library/venv.html) say plainly "virtual
# environments are conventionally named `.venv` or `venv`", Flask's official
# installation guide is the only one of the two that happens to use `.venv`,
# and GitHub's own `github/gitignore` `Python.gitignore` template (the
# pattern list `gh repo create --gitignore Python`/the GitHub web UI's "Add
# .gitignore" button writes into new repos) lists `.venv`, `env/`, `venv/`,
# `ENV/`, `env.bak/`, and `venv.bak/` side by side as equally-real virtualenv
# directory names. Before this fix, only the dot-prefixed form was pruned, so
# a real, common non-dot-prefixed virtualenv sitting inside the scanned tree
# (e.g. the exact `python3 -m venv venv` from Python's own docs) wasn't — and
# a virtualenv can genuinely contain an installed package's own bundled
# manifest, the same false-signal shape `.venv` was already fixed for above:
# confirmed live, a fresh `venv` (no dot) with pandas installed carries
# `.../site-packages/pandas/pyproject.toml` (90 raw dependency specs, none
# of them related to the project actually being scanned) and
# `.../site-packages/numpy/f2py/setup.cfg`, both picked up by an unpruned
# `find_manifests` walk and both invisible to it once `venv` is pruned the
# same way `.venv` already is.
_SKIP_DIR_NAMES = {"node_modules", "venv", "env", "ENV", "venv.bak", "env.bak"}


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
            "(expected requirements.txt, pyproject.toml, package.json, Pipfile, "
            "setup.cfg, or a *.txt requirements file)"
        )
    try:
        return parser(path)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError, configparser.Error) as e:
        raise ManifestParseError(f"{path}: couldn't parse as {path.name} ({e})") from e
