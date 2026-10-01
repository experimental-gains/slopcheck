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


# pip's own requirements-file preprocessor (`req_file.py`'s `join_lines`,
# read directly from pip 25.1.1's source) joins a physical line ending in a
# trailing `\` with the line(s) that follow it into one logical line *before*
# any requirement is parsed out of it — and, separately, `pip-compile
# --generate-hashes` (part of the widely-used pip-tools) routinely emits
# exactly this shape for a spec too long to fit on one line, e.g. wrapping a
# long extras list so the version specifier lands on its own continuation
# line. Confirmed live (real pip 25.1.1, `pip install --dry-run -r` against
# a two-line file: `"totally-hallucinated-xyz-987 \\"` then `"    ==1.2.3"`):
# pip genuinely joins them into one requirement and fails resolving it
# exactly like any other hallucinated name ("ERROR: Could not find a version
# that satisfies the requirement totally-hallucinated-xyz-987==1.2.3 (from
# versions: none)").
#
# Before this fix, `_parse_requirements_txt` walked `text.splitlines()` with
# no continuation-joining at all: `_REQ_LINE_RE` has no alternative that
# matches a lone trailing "\", so a continued first line failed to match and
# was silently dropped, and a continuation line starting with the version
# specifier alone (no leading name character) never matches either — the
# name never became a `Dependency` at all, even though a real `pip install
# -r` genuinely tries to fetch it. (A continuation that only carries
# `--hash=...` flags after an already-complete `name==version` on the first
# line happened to keep working before this fix too, since `_REQ_LINE_RE`'s
# trailing `.*` greedily swallows the stray backslash on that first line —
# this fix doesn't change that case, just the one it was accidental for.)
#
# Mirrors pip's own `COMMENT_RE.match(line)` guard (a whole-line comment,
# even one ending in "\", is never continued) and pip's "no separator, plain
# concatenation" join (a continuation line's own leading whitespace is kept
# verbatim, exactly as pip's `"".join(new_line)` does it) — precise enough to
# match pip's real behavior for this format's ordinary use, without needing
# every other unrelated preprocessing step (env var expansion, full comment
# stripping) `join_lines` sits alongside in pip's own pipeline, which this
# module already applies its own way further down.
_WHOLE_LINE_COMMENT_RE = re.compile(r"^\s*#")


def _join_backslash_continuations(text: str) -> list[str]:
    lines: list[str] = []
    buffer: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.endswith("\\") and not _WHOLE_LINE_COMMENT_RE.match(raw_line):
            buffer.append(raw_line[:-1])
            continue
        if buffer:
            buffer.append(raw_line)
            lines.append("".join(buffer))
            buffer = []
        else:
            lines.append(raw_line)
    if buffer:
        lines.append("".join(buffer))
    return lines


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
#
# pip's own requirements-file option parser is a plain `optparse.OptionParser`
# (confirmed by reading `pip._internal.req.req_file.SUPPORTED_OPTIONS`
# directly), which accepts a short option's argument either space-separated
# (`-r base.txt`) *or* concatenated with no separator at all (`-rbase.txt`),
# and a long option's argument either space-separated or joined with `=`
# (`--requirement=base.txt`) — optparse's standard, documented behavior for
# any option that takes a value, not something specific to pip. Live-verified
# directly against installed pip 25.1.1
# (`pip._internal.req.req_file.parse_requirements`): both `-rbase.txt` and
# `--requirement=base.txt` genuinely resolved and recursed into `base.txt`
# exactly like the spaced form already handled below, while
# `--requirementbase.txt` (long option with no separator at all — not a
# form optparse supports for long options) correctly raised
# `RequirementsFileParseError` in real pip, so it's deliberately left
# unmatched here too. Before this fix, the regex required literal
# whitespace after the flag (`\s+`), so either real, accepted variant fell
# through unmatched — not `-r`/`--requirement` (recognized), not `-c`/
# `--constraint` (recognized), not `-e`/`--` (recognized), and not a plain
# name (`_REQ_LINE_RE` needs a leading alnum char, but the line still starts
# with `-`) — so the whole line was silently dropped and the nested file's
# dependencies, hallucinated or not, were never checked at all: the same
# silent false-all-clear shape as every other unhandled-directive gap this
# module has already fixed, just for a separator variant instead of a
# missing directive.
_REQ_FILE_RE = re.compile(r"^(?:-r\s*|--requirement(?:\s+|=))(?P<target>\S.*?)\s*$")

# pip's constraints-file directive (`-c`/`--constraint`). Deliberately NOT
# recursed into like `-r` above: per pip's own documentation, a name that
# appears *only* in a constraints file and nowhere else in the resolved
# requirement set has no effect at all — pip won't install it. Treating a
# constraints-only entry as a real dependency would risk a false positive
# (flagging a name that's merely a version pin for some other project's
# transitive dependency, never installed by this one) rather than fixing a
# false negative, so it stays skipped.
#
# Accepts the same no-space/`=` separator variants as `_REQ_FILE_RE` above,
# for the same live-verified reason (real pip's `-cbase.txt`/
# `--constraint=base.txt` both resolve identically to the spaced form) —
# kept symmetric so a concatenated/`=`-form constraints directive is
# recognized and skipped explicitly, the same as the spaced form, rather
# than happening to be dropped for the unrelated reason that it doesn't
# match `_REQ_LINE_RE` either.
_CONSTRAINT_FILE_RE = re.compile(r"^(?:-c\s*|--constraint(?:\s+|=))(?P<target>\S.*?)\s*$")


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


def _parse_requirement_lines(
    lines: list[str], base_dir: Path, source: str, seen: set[Path]
) -> tuple[list[Dependency], set[Path]]:
    """Shared pip-requirements-line logic: name extraction plus -r/-c/-e/URL handling.

    Factored out of `_parse_requirements_txt` so `parse_environment_yml`'s conda
    `pip:` block (a real pip requirement per line, per conda's own
    `conda/env/installers/pip.py install()`, just sourced from a YAML sequence
    instead of a `.txt` file's lines) gets the exact same `-r`/`--requirement`
    recursion, `-c`/`--constraint` skip, and editable/URL handling as an
    ordinary `requirements.txt` — see `parse_environment_yml`'s own docstring
    for the live-verified gap this closed.

    `base_dir` is where a `-r`/`-c` target's relative path is resolved against
    -- the referencing requirements.txt's own directory for a nested `-r`, or
    (for the conda case) the `environment.yml`'s own directory, matching
    conda's real `get_pip_workdir()` exactly (see `parse_environment_yml`).
    `source` is the `Dependency.source` recorded for a name matched directly
    from `lines` (a nested `-r` target still gets its own file's path as
    `source`, via the recursive `_parse_requirements_txt` call, not this
    value).
    """
    deps: list[Dependency] = []
    for raw_line in lines:
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
            target = base_dir / req_match.group("target")
            nested_deps, seen = _parse_requirements_txt(target, seen)
            deps.extend(nested_deps)
            continue
        if _CONSTRAINT_FILE_RE.match(line) or line.startswith(("-e ", "--")):
            continue
        if "://" in line:
            continue
        match = _REQ_LINE_RE.match(line)
        if match:
            deps.append(Dependency(match.group("name"), "pypi", source))
    return deps, seen


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

    return _parse_requirement_lines(_join_backslash_continuations(text), path.parent, str(path), seen)


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
    """Flatten Hatch's `[tool.hatch.env].requires` and per-environment `dependencies`/`extra-dependencies` into requirement specs.

    Hatch (https://hatch.pypa.io/latest/config/environment/overview/,
    the PyPA-recommended build backend/env manager) has two distinct
    tables of its own, neither PEP 621 nor PEP 735 and neither read by any
    function above: `[tool.hatch.env] requires` lists environment-plugin
    packages Hatch itself installs (via its own resolver, not even pip)
    before it can even parse the rest of an environment's config, and each
    `[tool.hatch.envs.<name>]` lists plain PEP 508 requirement strings
    installed into that named environment under *two* separate fields:
    `dependencies` and `extra-dependencies`. Confirmed live with Hatch
    1.18.1 against a scratch project: a `[tool.hatch.envs.default]
    dependencies = ["totally-hallucinated-package-xyz-123"]` table made
    `hatch env create` genuinely fail resolving the fake name from PyPI
    ("Could not find a version that satisfies the requirement ... (from
    versions: none)"), and a separate `[tool.hatch.env] requires =
    ["totally-hallucinated-hatch-plugin-xyz-987"]` made the same command
    fail even earlier, while syncing environment plugin requirements
    ("No solution found when resolving dependencies ... was not found in
    the package registry"). Before that fix neither table had any reader
    here at all, so a hallucinated name planted in either one — a real
    place for one to land, since Hatch is the build backend this project's
    own dependents are as likely to use as Poetry or PDM — sailed through
    unchecked even though a real `hatch env create` genuinely tries to
    install it. Unlike Poetry's/uv's dependency tables, Hatch's own docs
    only document these two fields as plain requirement-string lists (no
    git/path/url table form), so no registry-source filtering is needed
    here the way `_is_poetry_registry_dep`/`_is_uv_registry_source` do it
    for their own tables.

    `extra-dependencies` (https://hatch.pypa.io/latest/config/environment/overview/#dependencies,
    "If you define environments with dependencies that only slightly
    differ from their inherited environments, you can use the
    `extra-dependencies` option to avoid redeclaring the `dependencies`
    option") is Hatch's own documented way to add packages to an
    environment that inherits from another (e.g. from `default`) without
    repeating its whole `dependencies` list. Hatch's own
    `environment_dependencies_complex` (read directly from installed
    Hatch 1.18.1's `hatch/env/plugin/interface.py`) resolves this field
    through the exact same validation and dependency-object construction
    as `dependencies` itself — both fields are simply iterated in the same
    loop (`for option in ("dependencies", "extra-dependencies")`), with no
    difference in how either is installed. This function used to only
    read `dependencies`, missing `extra-dependencies` entirely, even
    though it's an ordinary field on the *same* env table this function
    already reads, not a feature of a different table. Confirmed real and
    current, not hypothetical: live-verified against real Hatch 1.18.1 —
    a scratch project with `[tool.hatch.envs.default] dependencies =
    ["requests"]` and `[tool.hatch.envs.experimental] extra-dependencies =
    ["totally-hallucinated-hatch-extradep-xyz-123"]` made `hatch env
    create experimental` genuinely try (and fail) to resolve the fake name
    from PyPI ("Could not find a version that satisfies the requirement
    totally-hallucinated-hatch-extradep-xyz-123 (from versions: none)"),
    while the pre-fix parser here reported zero dependencies for it at
    all — the same silent false-all-clear shape as every other
    unhandled-field gap already fixed in this module, just for a sibling
    field on a table this function already partially reads.
    """
    hatch = data.get("tool", {}).get("hatch", {})
    names = list(hatch.get("env", {}).get("requires", []))
    for env in hatch.get("envs", {}).values():
        if isinstance(env, dict):
            names.extend(env.get("dependencies", []))
            names.extend(env.get("extra-dependencies", []))
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

    `git` isn't the only VCS key Pipenv recognizes, though: its own schema
    (`pipenv.vendor.plette.models.packages.PackageSpecfiers`, read directly
    from Pipenv 2026.8.0's installed source) lists `git`, `svn`, `hg`, and
    `bzr` side by side as equally valid dependency-spec keys — Pipenv's own
    `VCS_LIST` constant (`pipenv/utils/constants.py`) is exactly `("git",
    "svn", "hg", "bzr")`, and Pipenv treats all four identically when
    deciding whether an entry is VCS-sourced rather than index-sourced
    (`pipenv/utils/dependencies.py`'s own `is_vcs()` helper: `any(key for
    key in pipfile_entry if key in VCS_LIST)`). Confirmed live (Pipenv
    2026.8.0, `pipenv lock -v` against a Pipfile with `totally-hallucinated-
    svn-test-xyz-123 = {svn = "svn://127.0.0.1:9/repo"}`): Pipenv genuinely
    dispatched to pip's own Subversion VCS backend
    (`unpack_vcs_link`/`subversion.py`'s `fetch_new`, which tried to run the
    `svn` command) and never queried PyPI for that name at all — the same
    non-registry-source shape as `git`, just for a different backend.
    Before this fix, only `git`/`path`/`file` opted a spec out of the
    registry check here, so an `svn`/`hg`/`bzr`-sourced Pipfile dependency
    (a real, still-current Pipenv feature, not a removed one) was sent to
    PyPI and reported as a plain `not_found` hallucination whenever the
    name wasn't independently published there — a false positive on a
    dependency a real `pipenv install`/`pipenv lock` resolves fine via its
    own VCS.
    """
    if isinstance(spec, dict):
        return not any(key in spec for key in ("git", "svn", "hg", "bzr", "path", "file"))
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


def _is_pylock_registry_package(package: dict) -> bool:
    """Whether a `pylock.toml` `[[packages]]` entry names something pip installs off the index.

    PEP 751 (https://packaging.python.org/en/latest/specifications/pylock-toml/)
    lets a locked package's actual file(s) come from a version-control checkout
    (`packages.vcs`) or a local directory (`packages.directory`, how a locked
    project's own editable/source-tree install is recorded) instead of a
    downloadable archive — the same non-registry-source situation already
    handled for Poetry's/uv's/Pipfile's own git/path table forms above.
    `packages.archive`/`packages.sdist`/`[[packages.wheels]]` all name a
    downloadable *file* instead, almost always (for a lock file a real locker
    tool generated) one it already fetched from a real index while resolving
    the lock, so those are left to the ordinary registry check.
    """
    return not any(key in package for key in ("vcs", "directory"))


def parse_pylock_toml(path: Path) -> list[Dependency]:
    """Parse a PEP 751 `pylock.toml`/`pylock.<name>.toml` lock file.

    PEP 751 (accepted 2025) standardizes a lock-file format recording the
    exact resolved set of packages for reproducible installs, one `[[packages]]`
    table per package with a required, already PEP-503-normalized `name` key.
    pip 26.1 (April 2026) shipped real, current — if still explicitly labeled
    "experimental" — support for installing directly from one via `pip install
    -r pylock.toml`, an alternative to `-r requirements.txt`; uv/PDM/Pipenv can
    already export to this format too. Before this fix, neither `PARSERS` nor
    `find_manifests` recognized this filename at all (and `parse_manifest`'s
    `.txt`-suffix fallback doesn't apply either, since a lock file is TOML),
    so a project locked this way had every name in it — the tool's entire job
    — silently never checked: the same "0 dependencies checked, all clean"
    false-all-clear already fixed here for `Pipfile`/`setup.cfg`/Hatch/PDM's
    legacy dev-dependencies table/PEP 735 dependency-groups, just for a format
    that didn't exist yet when any of those were fixed.

    Confirmed real and live, not hypothetical: generated an actual
    `pylock.toml` with real pip 26.2.1 (`pip lock -r req.txt -o pylock.toml`
    against a plain `requests==2.32.3` requirement) and got back exactly the
    `[[packages]] name = "..." version = "..." [[packages.wheels]] url =
    "https://files.pythonhosted.org/..."` shape this function reads. Hand-
    added a `[[packages]]` entry naming a hallucinated package with no
    `vcs`/`directory`/`archive`/`sdist`/`wheels` at all made real `pip install
    --dry-run -r` (both the bare `-r pylock.toml` name and the
    `pylock.<name>.toml` variant PEP 751 also allows) refuse outright with
    "Invalid pylock file ...: Exactly one of vcs, directory, archive must be
    set if sdist and wheels are not set" — real pip Fatals before ever
    resolving anything, so that shape can't reach a false negative here
    either way. A hallucinated package *with* a fabricated `[[packages.wheels]]`
    URL, the shape a hand-edited or LLM-authored lock file would actually
    produce, installs (or 404s) by that literal URL alone, with zero query to
    PyPI for the name — confirmed live the same way — but that URL is exactly
    what a real locker tool (`pip lock`, `uv export --format pylock.toml`)
    only ever writes *after* successfully resolving the name from a real
    index, so checking the name for existence/recency here is exactly as
    meaningful as it is for an ordinary `requirements.txt`/`pyproject.toml`
    entry, catching the same slopsquatting shape further down the pipeline
    (a lock file pinning a real-but-newly-squatted transitive dependency an
    LLM suggested, faithfully resolved and hashed in by the locker itself).
    """
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    deps = []
    for package in data.get("packages", []):
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        if not isinstance(name, str) or not name:
            continue
        if not _is_pylock_registry_package(package):
            continue
        deps.append(Dependency(name, "pypi", str(path)))
    return deps


# PEP 751's own filename rule: bare "pylock.toml", or "pylock.<name>.toml" for
# a named/multi-use lock file (e.g. "pylock.dev.toml") -- both forms are real,
# equally valid targets for `pip install -r`. Checked separately from the
# plain `PARSERS` dict (`find_manifests`/`parse_manifest` below) since the
# variable "<name>" segment can't be a literal dict key.
_PYLOCK_FILENAME_RE = re.compile(r"^pylock\.([^.]+\.)?toml$")


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


def _setup_cfg_self_referential_name(parser: configparser.ConfigParser) -> set[str]:
    """setup.cfg's own `[metadata] name`, PEP 503-normalized, as a self-referential-extra skip-set.

    `parse_pyproject_toml` already skips a PEP 621 project's own
    self-referential extra (`_self_referential_name` above: an
    `[project.optional-dependencies]`/`[dependency-groups]` entry naming the
    *current* project with a different combination of its own extras, e.g.
    `all = ["your-project-name[gui, cli]"]`, so an umbrella extra doesn't
    need a hand-maintained copy of every other extra's dependency list). That
    same pattern is exactly as legal, and exactly as common, for a legacy
    `setup.cfg`-based project's `[options.extras_require]` — it's a property
    of pip's own dependency resolution (a self-named `Requires-Dist` in the
    built package's own metadata is always satisfied by the package already
    being installed, regardless of which manifest format declared it), not
    something specific to PEP 621's declaration syntax.

    Confirmed live (setuptools 84.0.0, real pip 25.x, a from-scratch
    `setup.cfg`-only project — `[metadata] name = totally-hallucinated-
    selfref-test-xyz-123`, `[options.extras_require] all =
    totally-hallucinated-selfref-test-xyz-123[gui]`, `gui = pillow`, no PEP
    621 `[project]` table at all): `pip install --dry-run -v ".[all]"`
    resolved the `all`/`gui` extras and installed `pillow` fine, never
    issuing a single request for `totally-hallucinated-selfref-test-xyz-123`
    itself — the self-reference is satisfied locally by the package being
    built, exactly like the PEP 621 case. Before this fix, `parse_setup_cfg`
    had no skip-set at all (unlike `parse_pyproject_toml`), so this same
    self-reference was emitted as an ordinary dependency and flagged
    `not_found` for any not-yet-published project using this pattern — the
    exact false positive `_self_referential_name` already exists to prevent
    for pyproject.toml, just never ported to this sibling parser for the
    older manifest format. Matched PEP 503-normalized, the same rule
    `_self_referential_name` and every other name-equality check in this
    module uses, since a `[metadata] name` value can differ in case/
    separators from how it's written inside `[options.extras_require]`.
    """
    if not parser.has_option("metadata", "name"):
        return set()
    name = parser.get("metadata", "name")
    return {_normalize_name(name)} if name else set()


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

    `[metadata] name`'s own self-referential-extra uses are skipped from
    `install_requires`/`[options.extras_require]` the same way
    `parse_pyproject_toml` skips PEP 621's equivalent — see
    `_setup_cfg_self_referential_name`. Deliberately NOT applied to
    `setup_requires`: that field is resolved by a separate isolated
    build-environment install step, before the package's own metadata (and
    thus its own extras) exists at all, so a self-reference there wouldn't
    resolve locally the way it does in the two fields above — mirroring how
    `parse_pyproject_toml` likewise never filters `[build-system] requires`
    against its own self-referential-name skip-set.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(path.read_text(encoding="utf-8-sig"), source=str(path))
    skip_names = _setup_cfg_self_referential_name(parser)

    deps = []
    if parser.has_option("options", "install_requires"):
        for dep in _setup_cfg_requirements_value(parser.get("options", "install_requires"), path):
            if _normalize_name(dep.name) not in skip_names:
                deps.append(dep)
    if parser.has_option("options", "setup_requires"):
        deps.extend(_setup_cfg_list_deps(parser.get("options", "setup_requires"), path))
    if parser.has_section("options.extras_require"):
        for value in parser["options.extras_require"].values():
            for dep in _setup_cfg_requirements_value(value, path):
                if _normalize_name(dep.name) not in skip_names:
                    deps.append(dep)
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


def _poetry_non_registry_names(data: dict) -> set[str]:
    """Names Poetry 2.0+ enriches a PEP 621 `[project.dependencies]` entry's *source* for.

    Poetry 2.0 (https://python-poetry.org/blog/announcing-poetry-2.0.0/,
    "Migration to the new project sections") made `[project.dependencies]`
    the *primary* declaration for a Poetry-managed project, with
    `[tool.poetry.dependencies]` now used only to attach Poetry-specific
    metadata (a git/path/url source, in particular) onto a dependency
    already named in `[project.dependencies]` — its own migration guide
    gives exactly this example: `dependencies = ["poetry-core"]` in
    `[project]` alongside `[tool.poetry.dependencies] poetry-core = {git =
    "https://github.com/python-poetry/poetry-core.git"}`.

    Confirmed by reading `poetry.core.factory.Factory._configure_package_dependencies`
    (poetry-core, installed via a scratch `pip install poetry` — version
    2.5.1) directly: every `[project.dependencies]`/`[project.optional-
    dependencies]` entry is added to the main dependency group first (as a
    plain `Dependency.create_from_pep_508` object), and *then*, if
    `[tool.poetry.dependencies]` is present, each of its entries is added to
    the *same* group as a separate "poetry dependency" via
    `add_poetry_dependency`. `DependencyGroup.dependencies_for_locking` (the
    property actually consulted during `poetry lock`/`poetry install`) then
    merges the two lists by name: any PEP 621 entry whose name also appears
    among the poetry-specific ones gets "enriched" with that entry's
    constraint (a git/path/url source, in this case) instead of keeping its
    own bare PEP 508 spec.

    Live-verified against real Poetry 2.5.1 (`poetry lock -vvv`, an
    unreachable-vs-real-repo technique like the rest of this file's private-
    registry checks): a scratch `pyproject.toml` with `[project] dependencies
    = ["requests"]` plus `[tool.poetry.dependencies] requests = {git =
    "https://github.com/psf/requests.git"}` made `poetry lock` clone the git
    repo for `requests` and never issue a single `pypi.org` request for that
    name — `pypi.org` was hit only for `requests`' own transitive PyPI
    dependencies (certifi, urllib3, idna, charset-normalizer), confirming the
    git source fully replaces PyPI resolution for the overridden name itself.
    Before this fix, `_pep621_deps` still emitted the bare `"requests"`
    string from `[project.dependencies]`, and nothing cross-referenced
    `[tool.poetry.dependencies]`'s own git/path/url entries against it the
    way `_uv_non_registry_names` already does for `[tool.uv.sources]` — the
    same "derived skip-set built for one override mechanism doesn't cover a
    different, newer one with the identical shape" gap, so a real,
    git-sourced dependency (using Poetry's own current, documented pattern
    for pinning a fork/patch of a PyPI package) was checked against PyPI and
    flagged `not_found` whenever the override name wasn't independently
    published there too.

    Reuses `_is_poetry_registry_dep` (already handles the multiple-
    constraints list form and the git/path/url key set) rather than
    duplicating its logic; only `[tool.poetry.dependencies]` is read here —
    `[tool.poetry.dev-dependencies]`/`[tool.poetry.group.*.dependencies]`
    only ever enrich the *legacy* (non-PEP-621) declaration path `_poetry_deps`
    already reads directly, so including them here would just be inert
    (their names never coincide with anything `_pep621_deps` emits from
    `[project.optional-dependencies]`, which is PEP 621's own extras
    mechanism, distinct from Poetry's dependency groups).
    """
    poetry = data.get("tool", {}).get("poetry", {})
    return {
        _normalize_name(name)
        for name, spec in poetry.get("dependencies", {}).items()
        if not _is_poetry_registry_dep(spec)
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
    # entries, [tool.poetry.dependencies]'s equivalent git/path/url overrides
    # of a same-named [project.dependencies] entry — see
    # _poetry_non_registry_names — and the project's own self-referential-
    # extra name) only means something for the *regular* dependency resolver
    # — uv/pip/Poetry reading [project.dependencies]/[project.optional-
    # dependencies]/[dependency-groups]/[tool.pdm.dev-dependencies]/
    # [tool.poetry.*], the tables these two override mechanisms actually
    # apply to. [build-system] requires and Hatch's [tool.hatch.env]/
    # [tool.hatch.envs.*] tables are resolved by two completely separate
    # mechanisms that never consult [tool.uv.sources] or
    # [tool.poetry.dependencies] at all: a PEP 517 isolated build environment
    # (plain pip, with no notion of uv/Poetry-specific config) and Hatch's
    # own env manager. Confirmed live (venv + pip 25.x, this box): a
    # pyproject.toml with `[tool.uv.sources] totally-hallucinated-buildreq-
    # xyz-123 = { path = "./local-pkg" }` and `[build-system] requires =
    # ["setuptools", "totally-hallucinated-buildreq-xyz-123"]` made `pip
    # install .` genuinely try to fetch that name from PyPI while installing
    # build dependencies and fail ("Could not find a version that satisfies
    # the requirement ... (from versions: none)") — pip's build-isolation
    # step never parses [tool.uv.sources] (and, by the same reasoning,
    # doesn't invoke Poetry's own resolver either, so a [tool.poetry.
    # dependencies] override of the identical name wouldn't apply to a
    # [build-system] requires entry sharing it). Before the uv fix, both
    # tables' output was folded into the same raw_specs list as the
    # project-level tables and filtered through the same skip_names set, so
    # a name that merely happened to collide with an unrelated skip-set
    # entry (or the project's own name) got silently skipped here too — the
    # same false negative _build_system_deps's/_hatch_deps's own docstrings
    # already describe (neither participates in [tool.uv.sources] or
    # [tool.poetry.dependencies] overrides), just not actually enforced by
    # this function until now.
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    raw_specs = (
        _pep621_deps(data)
        + _poetry_deps(data)
        + _dependency_groups_deps(data)
        + _pdm_dev_deps(data)
    )
    skip_names = (
        _uv_non_registry_names(data) | _self_referential_name(data) | _poetry_non_registry_names(data)
    )
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
#
# "gist:" is npm's fourth documented git-host shorthand (npm's own docs list
# it right alongside "github:"/"gitlab:"/"bitbucket:": "you may also specify
# the gist:, bitbucket:, gitlab:, and github: prefixes explicitly"), and
# npm-package-arg's `HostedGit.fromUrl()` (read directly from
# `/usr/share/nodejs/npm-package-arg/lib/npa.js` on this box) dispatches it
# to git exactly like the other three. Unlike them, though, a real gist
# reference is just a bare gist ID with no "/" at all (e.g. "gist:
# 101a11beef") — `_npm_looks_like_hosted_git_or_path`'s blanket "/" fallback
# below, which is what catches the *other* three hosts' bare/prefixed
# shorthand, never fires for it, so it needs its own explicit prefix here
# instead. Confirmed live (npm 11.20.0, `npm install`): a hallucinated
# dependency name paired with a "gist:"-prefixed version value made npm run
# `git --no-replace-objects ls-remote ssh://git@gist.github.com/<id>.git`
# and never contact the npm registry for that name at all, regardless of
# whether the gist exists — the exact same false-positive shape already
# fixed for "github:"/"gitlab:"/"bitbucket:"/bare shorthand.
_NON_REGISTRY_PREFIXES = ("workspace:", "file:", "link:", "portal:", "git:", "git+", "github:", "gist:")

_NPM_ALIAS_PREFIX = "npm:"

_NPM_PATCH_PREFIX = "patch:"

# npm-package-arg's own `isFilespec` regex is ASCII-only (`[a-zA-Z]:`, not a
# Unicode letter class) even on non-Windows platforms — confirmed by reading
# its source directly (see `_npm_looks_like_hosted_git_or_path`'s docstring).
# A non-ASCII "letter" (e.g. "é:foo") doesn't match it (nor npm's other
# protocol-sniffing regexes, all similarly `[a-z]`-only), so it falls through
# to a real registry lookup in actual npm — `str.isalpha()` would wrongly
# return True for it and over-match, so this is spelled as an explicit ASCII
# class instead.
_NPM_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


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

    A version value starting with a literal `.` is a local-path reference
    too, even with no `/` anywhere in it — reading `resolve()`'s own source
    shows it checks this (`isFilespec`, `/^(?:[.]|~[/]|[/]|[a-zA-Z]:)/`)
    *before* it ever reaches the `HostedGit`/slash fallback above, so `"."`
    and `".."` alone both qualify, not just multi-segment paths like
    `"../foo"` (already caught by the `"/" in version` check). Confirmed
    live end-to-end (npm 9.15.0, real `npm install`, no `--dry-run`): a
    scratch `package.json` with `"totally-hallucinated-selfref-xyz-987":
    "."` in `devDependencies` installed cleanly (`node_modules/totally-
    hallucinated-selfref-xyz-987` created, pointing back at the project's
    own directory) with zero requests to `registry.npmjs.org` for that name
    anywhere in a full `--loglevel silly` trace — the only registry hit was
    the unrelated bulk security-advisory POST every `npm install` makes.
    Before this fix, `_npm_non_registry_version(".")` returned `False`
    (no `/`, no recognized prefix), so slopcheck sent this name to the
    public npm registry and would report it "NOT FOUND" — a false positive
    on a dependency a real `npm install` resolves entirely locally.

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

    The `.`/`..` fix above quoted `isFilespec`'s full regex
    (`/^(?:[.]|~[/]|[/]|[a-zA-Z]:)/`) but only ever ported its first
    alternative (`[.]`) — the last one, a bare drive-letter prefix
    (`[a-zA-Z]:`), was left uncovered even though it's the exact same
    regex, checked by the exact same `resolve()` call, before npm ever
    reaches the `HostedGit`/`isURL`/slash fallback this function otherwise
    models. It matches unconditionally, on every platform: `hasSlashes` is
    OS-gated in npm-package-arg's own source (backslash counts as a
    separator only on Windows), but `isFilespec`'s `[a-zA-Z]:` alternative
    is not — confirmed live on this box (Linux, real npm 9.2.0, `npm
    install --dry-run --loglevel silly`) that a fake dependency under
    `"totally-hallucinated-name-xyz-123": "C:\\Users\\dev\\local-lib"`
    (a real, plausible shape: a Windows developer's local-path override,
    committed as-is and later scanned by slopcheck on Linux CI) made npm
    attempt to `open('/C:/Users/dev/local-lib/package.json')` and fail with
    `ENOENT` — never a single request to `registry.npmjs.org` for that
    name. Same result for a lowercase drive letter with no backslash at
    all (`"c:foo"` -> `open('/tmp/.../c:foo/package.json')`), confirming
    it's the bare `<letter>:` prefix that triggers it, not the backslashes
    or the specific drive letter. Before this fix, `_npm_non_registry_version
    ("C:\\Users\\dev\\local-lib")` returned `False` (no `/`, doesn't start
    with `.`), so slopcheck sent the name to the public registry and
    reported a real, resolvable (if broken on this platform) local
    reference as a plain `NOT FOUND` hallucination.
    """
    if version.startswith(_NPM_PATCH_PREFIX):
        return False
    return "/" in version or version.startswith(".") or bool(_NPM_DRIVE_LETTER_RE.match(version))


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
        names = _workspace_member_names_from_patterns(path.parent, patterns)
        if names:
            pairs.append((path.parent, names))
    return pairs


def _workspace_member_names_from_patterns(root: Path, patterns: list[str]) -> set[str]:
    """Resolve a list of workspace glob patterns (rooted at `root`) to the declared `name` of each matching member.

    Shared by `npm_workspace_member_names` (patterns come from a
    package.json's own `workspaces` field, matched relative to that same
    file's directory) and `pnpm_workspace_member_names` (patterns come from
    a sibling `pnpm-workspace.yaml`'s `packages:` list, matched relative to
    *its* directory) — both ultimately reduce to "glob this pattern, read
    each match's own package.json `name`," just sourced from a different
    file for pnpm.
    """
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
    return names


_PNPM_WORKSPACE_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")
_PNPM_WORKSPACE_LIST_ITEM_RE = re.compile(r"^-\s*(.*)$")


def _pnpm_workspace_patterns(text: str) -> list[str]:
    """Extract glob patterns from a `pnpm-workspace.yaml`'s top-level `packages:` block-sequence list.

    pnpm workspaces (https://pnpm.io/workspaces) are declared entirely
    differently from npm's/Yarn Classic's `package.json` `workspaces` field
    (`_npm_workspace_patterns` above): pnpm requires a *separate* file,
    `pnpm-workspace.yaml`, at the workspace root, with its own `packages:`
    key listing the same kind of glob patterns
    (`packages:\\n  - 'packages/*'`) — pnpm does not read `package.json`'s
    `workspaces` field at all. Not a general YAML parser, the same "hand-
    parse the subset of syntax that matters" approach already used for
    Yarn Berry's own YAML `.yarnrc.yml` (`_parse_yarnrc_registries`): a real
    `pnpm-workspace.yaml`'s `packages:` value is always a plain block
    sequence of quoted or bare glob strings, confirmed against pnpm's own
    generated file (`pnpm init`) and its docs' own examples. Flow-style
    (`packages: ['packages/*']`) is a known gap, not worth the added
    complexity for a form pnpm's own tooling never generates, mirroring
    `_parse_yarnrc_registries`'s identical documented gap for `.yarnrc.yml`.
    """
    patterns: list[str] = []
    in_packages = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            top_match = _PNPM_WORKSPACE_TOP_KEY_RE.match(stripped)
            in_packages = bool(top_match) and top_match.group(1) == "packages" and not top_match.group(2)
            continue
        if not in_packages:
            continue
        item_match = _PNPM_WORKSPACE_LIST_ITEM_RE.match(stripped)
        if item_match:
            item = item_match.group(1).strip().strip("\"'")
            if item:
                patterns.append(item)
    return patterns


def pnpm_workspace_member_names(pnpm_workspace_paths: list[Path]) -> list[tuple[Path, set[str]]]:
    """For every `pnpm-workspace.yaml` found, the local package names it resolves without the registry.

    The pnpm analog of `npm_workspace_member_names` above, for pnpm's own
    independent workspace-declaration mechanism (a sibling
    `pnpm-workspace.yaml`, not `package.json`'s `workspaces` field — see
    `_pnpm_workspace_patterns`). Unlike npm/Yarn Classic, where an ordinary
    semver-range dependency on a workspace sibling resolves locally
    *unconditionally*, pnpm's default (`link-workspace-packages` unset,
    which defaults to `false`) actually sends such a dependency to the
    registry and fails it exactly like slopcheck already reports it —
    confirmed live (pnpm 9.15.0, a from-scratch two-package pnpm workspace:
    root `package.json` `devDependencies: {"totally-hallucinated-pnpm-
    workspace-xyz-123": "^1.0.0"}`, `pnpm-workspace.yaml` `packages: -
    'packages/*'`, member `packages/internal-lib/package.json` `name:
    "totally-hallucinated-pnpm-workspace-xyz-123", private: true`): a plain
    `pnpm install` genuinely issued `GET https://registry.npmjs.org/
    totally-hallucinated-pnpm-workspace-xyz-123` and failed with a real
    404 — matching slopcheck's own `not_found` verdict for that shape.

    Setting `link-workspace-packages=true` in `.npmrc` (a real, current,
    documented pnpm setting: https://pnpm.io/settings#linkworkspacepackages
    — the *default* before pnpm 8, so still commonly carried over into
    older or migrated monorepos' checked-in `.npmrc`) changes this:
    confirmed live, the identical scan with only that one line added to a
    root `.npmrc` made the same `pnpm install` resolve the dependency
    entirely locally (symlinked into `node_modules`, "Already up to date",
    zero registry requests for that name) instead. Whether that setting is
    present isn't threaded through here, the same safe-over-approximation
    choice `npm_workspace_member_names`'s own docstring already makes for
    a mismatched-but-still-locally-resolved version range: a name that is
    a real pnpm workspace member's own declared name is essentially never
    going to *also* be an independently-hallucinated name some other
    manifest in the same scan invents, so unconditionally treating it as
    local-only costs little detection power while fixing the real,
    live-verified false positive for any pnpm monorepo that does have the
    setting on. Before this fix, `scan()` had no notion of
    `pnpm-workspace.yaml` at all, so a pnpm monorepo using this real,
    documented setting had every private, unpublished workspace member
    referenced with a plain semver range reported as a plain `not_found`
    hallucination — the exact failure shape `npm_workspace_member_names`
    already fixed for npm/Yarn Classic, just for pnpm's own, differently-
    shaped workspace-declaration file.
    """
    pairs: list[tuple[Path, set[str]]] = []
    for path in pnpm_workspace_paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        patterns = _pnpm_workspace_patterns(text)
        if not patterns:
            continue
        names = _workspace_member_names_from_patterns(path.parent, patterns)
        if names:
            pairs.append((path.parent, names))
    return pairs


def _pdm_workspace_patterns(data: dict) -> list[str]:
    """Extract glob/literal member patterns from a pyproject.toml's `[tool.pdm.workspace]`.

    PDM's own workspace feature (https://pdm-project.org/latest/usage/workspace/,
    "Added in 2.28.0") is a *third* monorepo-membership mechanism this module
    handles, structurally closer to pnpm's than to uv's: the root project's
    `[tool.pdm.workspace] members = ["packages/foo", "tools/*"]` names sibling
    directories (direct paths or glob patterns, mixed freely) that each carry
    their own `pyproject.toml`. Unlike uv (`[tool.uv.workspace]`, which requires
    a matching `[tool.uv.sources] name = { workspace = true }` entry before a
    plain dependency on a member resolves locally — confirmed live above, real
    `uv lock` Fatals without it), PDM's own docs give the member-dependency
    example as a bare `dependencies = ["bar"]`, no `[tool.pdm.sources]`
    involved at all: "Workspace members can depend on each other by package
    name... PDM resolves it from the workspace checkout as an editable
    package."
    """
    workspace = data.get("tool", {}).get("pdm", {}).get("workspace", {})
    if not isinstance(workspace, dict):
        return []
    return [m for m in workspace.get("members", []) if isinstance(m, str)]


def pdm_workspace_member_names(pyproject_paths: list[Path]) -> list[tuple[Path, set[str]]]:
    """For every pyproject.toml declaring `[tool.pdm.workspace]`, the local package names it resolves without the registry.

    Confirmed live (PDM 2.29.2, `pdm lock -v` against a from-scratch two-project
    workspace: root `pyproject.toml` with `dependencies =
    ["totally-hallucinated-pdm-workspace-xyz-123"]` and `[tool.pdm.workspace]
    members = ["packages/*"]`, member `packages/bar/pyproject.toml` naming
    itself `totally-hallucinated-pdm-workspace-xyz-123`, no `[tool.pdm.sources]`
    anywhere): `pdm lock` resolved the dependency entirely locally ("The file
    packages/bar is a local directory, use it directly" / "Adding new pin:
    totally-hallucinated-pdm-workspace-xyz-123 file:///${PROJECT_ROOT}/packages/bar")
    with zero PyPI requests. Before this fix, `parse_pyproject_toml` had no
    notion of `[tool.pdm.workspace]` at all, so a plain `[project.dependencies]`
    entry naming a real workspace sibling (PDM's own documented pattern, and
    the most natural way to write one — no special table needed, unlike uv)
    was checked against PyPI and reported `not_found` whenever the sibling's
    name wasn't independently published there — the exact same false-positive
    shape `npm_workspace_member_names`/`pnpm_workspace_member_names` already
    fix for their own ecosystems' workspace mechanisms.

    Each member directory's own declared name comes from its own
    `pyproject.toml`'s `[project.name]` (an ordinary PEP 621 project, unlike
    npm/pnpm's `package.json`), so this can't reuse
    `_workspace_member_names_from_patterns` (hardcoded to `package.json`/JSON)
    unchanged. Matched PEP 503-normalized by the caller
    (`cli._is_pdm_workspace_member`), the same rule every other PyPI name
    comparison in this module already uses, since a workspace member's
    `[project.name]` and the string naming it in `dependencies` can differ in
    case/separators.
    """
    pairs: list[tuple[Path, set[str]]] = []
    for path in pyproject_paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        patterns = _pdm_workspace_patterns(data)
        if not patterns:
            continue
        names: set[str] = set()
        for pattern in patterns:
            try:
                matches = list(path.parent.glob(pattern))
            except (ValueError, NotImplementedError):
                continue
            for member_dir in matches:
                member_pyproject = member_dir / "pyproject.toml"
                if not member_pyproject.is_file():
                    continue
                try:
                    member_data = tomllib.loads(member_pyproject.read_text(encoding="utf-8-sig"))
                except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                    continue
                name = member_data.get("project", {}).get("name")
                if isinstance(name, str):
                    names.add(name)
        if names:
            pairs.append((path.parent, names))
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
    # A resolutions entry's value can itself be an `npm:` alias — the same
    # aliasing syntax `dependencies`/`overrides` already resolve above —
    # substituting a *different* real package for the one the pattern's key
    # names. Before this fix, only the key (via `_yarn_resolution_target`)
    # was ever inspected; the value was never read at all, so an aliased
    # resolutions entry checked the wrong name entirely. Confirmed live
    # (Yarn Classic 1.22.22, `yarn install` against a scratch package.json
    # with `"resolutions": {"is-odd/**/is-number": "npm:totally-
    # hallucinated-slopcheck-test-xyz-42@1.0.0"}`): Yarn genuinely queried
    # `https://registry.yarnpkg.com/totally-hallucinated-slopcheck-test-
    # xyz-42` (a real npm registry mirror) and failed the install with a
    # real 404 for the hallucinated alias target — not for `is-number`, the
    # name the pre-fix code checked instead. A `patch:`-wrapped value (the
    # jest.js-derived shape `test_parse_package_json_strips_range_from_
    # yarn_resolution_key` already covers) is deliberately left to the
    # existing key-based path: unlike a bare `npm:` alias, its wrapped
    # target already matched the key's own name in every real example found
    # (jest's own package.json), so there's no live-verified case yet where
    # unwrapping it further would change which name gets checked.
    for pattern, value in data.get("resolutions", {}).items():
        if isinstance(value, str) and value.startswith(_NPM_ALIAS_PREFIX):
            deps.append(Dependency(_npm_alias_target(value), "npm", str(path)))
        else:
            deps.append(Dependency(_yarn_resolution_target(pattern), "npm", str(path)))
    return deps


_ENV_YML_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")
_ENV_YML_LIST_ITEM_RE = re.compile(r"^-\s*(.*)$")


def _environment_yml_pip_requirement_lines(text: str) -> list[str]:
    """Extract conda `environment.yml`'s nested `pip:` list as raw requirement-line strings.

    A conda environment file's own "mixed" dependency format
    (https://conda.io/projects/conda/en/latest/user-guide/tasks/manage-environments.html#create-env-file-manually)
    is a top-level `dependencies:` block sequence where every ordinary item
    names a conda package (`python=3.8.5`, `pytorch=1.11.0`), except one
    item, `pip:`, which itself carries a *nested* block sequence of PyPI
    package specs instead of naming a conda package at all -- e.g. real,
    currently-used `CompVis/latent-diffusion`:
    `dependencies:\\n  - python=3.8.5\\n  - pip=20.3\\n  - pip:\\n    -
    albumentations==0.4.3\\n    - -e git+https://...`. Not a general YAML
    parser, the same hand-parsed-subset approach already used for
    `pnpm-workspace.yaml`'s `packages:` list (`_pnpm_workspace_patterns`)
    and Yarn Berry's `.yarnrc.yml` -- a real `environment.yml`'s
    `dependencies:`/`pip:` values are always plain block sequences,
    confirmed against conda's own generated files and multiple real
    upstream repos. Flow-style (`dependencies: [python=3.8, {pip: [...]}]`)
    is a known gap, not worth the added complexity for a form conda's own
    tooling (`conda env export`) never generates, mirroring the pnpm/Yarn
    parsers' identical documented gap.
    """
    lines: list[str] = []
    in_dependencies = False
    pip_indent: int | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            top_match = _ENV_YML_TOP_KEY_RE.match(stripped)
            in_dependencies = bool(top_match) and top_match.group(1) == "dependencies" and not top_match.group(2)
            pip_indent = None
            continue
        if not in_dependencies:
            continue
        if pip_indent is not None and indent > pip_indent:
            item_match = _ENV_YML_LIST_ITEM_RE.match(stripped)
            if item_match:
                lines.append(item_match.group(1))
            continue
        pip_indent = None
        item_match = _ENV_YML_LIST_ITEM_RE.match(stripped)
        if item_match and item_match.group(1).strip() == "pip:":
            pip_indent = indent
    return lines


def parse_environment_yml(path: Path) -> list[Dependency]:
    """Parse a conda `environment.yml`/`environment.yaml`'s nested `pip:` dependency list.

    conda's own docs (link above) document this "mixed" format as the
    normal way to combine conda packages with PyPI-only ones in one
    environment file, and it's real, current, and common in ML/data-science
    repos that mix a conda-only dependency (`cudatoolkit`, `pytorch` pinned
    to a CUDA build) with ordinary PyPI packages that have no meaningful
    conda-channel equivalent. Before this fix, `environment.yml`/
    `environment.yaml` had no `PARSERS`/`find_manifests` entry at all --
    unlike `.yml`, there's no generic-suffix fallback the way `parse_manifest`
    treats any unmatched `.txt` file as `requirements.txt`-shaped -- so a
    conda-only project's entire PyPI dependency list, hallucinated names
    included, was silently never scanned. Confirmed against conda's own
    `conda/env/installers/pip.py` `install()`: every `pip:` entry is written
    verbatim into a temporary `requirements.txt` and passed to a real `pip
    install -U -r <tmpfile>` subprocess -- the exact same install path (and
    the same `-e`/`--requirement`/`--constraint`/URL directives) already
    handled for a standalone `requirements.txt` above, reused here rather
    than reimplemented. Only the nested `pip:` list is read; every other
    `dependencies:` item names a conda package resolved from a conda
    channel, not PyPI, and checking it against PyPI would be a false
    positive (a real, non-hallucinated conda-only package like
    `cudatoolkit` simply isn't a PyPI release at all).

    The paragraph above claimed the `pip:` list gets "the exact same
    `-r`/`--requirement`/`--constraint`/URL directives ... reused here rather
    than reimplemented" — that claim didn't actually hold: this function used
    to match each `pip:` line against `_REQ_LINE_RE` directly, with only an
    `-e `/`--`/`://` skip inlined by hand, and never called
    `_parse_requirements_txt`/`_REQ_FILE_RE` at all. `-r other.txt`/
    `--requirement other.txt` doesn't start with `-e `/`--` and doesn't match
    `_REQ_LINE_RE` either (no leading name character), so it fell through
    both checks and was silently dropped — not "handled the same way as
    `-e`/URL", just as unrecognized as a stray typo, even though a real
    nested pip requirements file referenced this way is genuinely followed.

    Confirmed live against conda's actual, current `install()`
    (conda/env/installers/pip.py, read directly off github.com/conda/conda's
    `main` branch): the `pip:` list is written into a temp requirements file
    inside `get_pip_workdir(args.file)` — `os.path.dirname(os.path.abspath(
    <path to this environment.yml>))`, i.e. this file's own directory, not a
    throwaway tmpdir — and `pip install -U -r <tmpfile>` is run with that
    same directory as `cwd`. Real pip's own requirements-file parser then
    recurses into any `-r`/`--requirement` target from there exactly as it
    would for a standalone `requirements.txt`, resolving a relative target
    against this file's directory. Live-verified end-to-end with a real
    Miniforge/conda 26.7.2 install (no mocking): an `environment.yml` whose
    `pip:` list was just `- -r requirements-dev.txt`, with a sibling
    `requirements-dev.txt` naming a hallucinated package, made
    `conda env create -f environment.yml` genuinely try (and fail) to `pip
    install` that hallucinated name — while pre-fix `parse_environment_yml`
    reported zero dependencies for the identical file, the same silent
    false-all-clear shape this function was originally written to close for
    `environment.yml` as a whole. Fixed by routing the extracted `pip:` lines
    through `_parse_requirement_lines` — the same per-line `-r`/`-c`/`-e`/URL
    logic `_parse_requirements_txt` already uses — with this file's own
    directory as the base for resolving a nested target, matching conda's
    real `get_pip_workdir()` exactly.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ManifestParseError(f"{path}: couldn't read ({e})") from e
    deps, _seen = _parse_requirement_lines(
        _environment_yml_pip_requirement_lines(text), path.parent, str(path), set()
    )
    return deps


def environment_yml_pip_lines(path: Path) -> list[str]:
    """The raw `pip:`-block line strings from a conda environment.yml, for private-index detection.

    `parse_environment_yml` already extracts these same lines to find
    dependency *names*; exposed here too so
    `private_registry.pip_private_index_configured` can scan the identical
    lines for a `-i`/`--extra-index-url`/`--index-url` directive. Confirmed
    directly from conda's own current `conda/env/installers/pip.py`
    (`install()`, read off github.com/conda/conda's `main` branch): every
    `pip:` entry is joined with `"\\n".join(specs)` and written *verbatim*,
    with no filtering of any kind, into a temp requirements file that a real
    `pip install -U -r <tmpfile> --exists-action=b` subprocess then reads —
    so a `-i`/`--extra-index-url` line sitting directly in the `pip:` list is
    exactly as effective as the same line at the top of a standalone
    `requirements.txt` (confirmed live with real pip 25.1.1: a requirements
    file whose first line is `-i http://<private-index>/simple` genuinely
    directs pip's lookup there). Before this fix, `pip_private_index_
    configured` only ever read file paths with a `.txt`/`.in` suffix
    (see `cli.scan`) — environment.yml's own YAML body was never one of
    those paths, so a directive living inline in its `pip:` block, as
    opposed to one living in a file reached via a nested `-r` target (see
    `environment_yml_files_touched` for that sibling case), was invisible to
    this scan no matter what. A private-only dependency named alongside such
    a directive was reported as a plain `not_found` hallucination instead of
    downgraded to `private`, even though real `conda env create` would
    genuinely resolve it from the configured index.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ManifestParseError(f"{path}: couldn't read ({e})") from e
    return _environment_yml_pip_requirement_lines(text)


def environment_yml_files_touched(path: Path) -> set[Path]:
    """Every requirements-format file reachable from environment.yml's `pip:` block via `-r`/`--requirement`.

    Mirrors `requirements_txt_files_touched`'s job for a standalone
    requirements.txt: a `-i`/`--extra-index-url` directive pip would honor
    for this scan can live in a *nested* file the `pip:` block's own `-r
    base.txt` line pulls in, not just inline in the block itself
    (`environment_yml_pip_lines` covers the inline case). Does not include
    `path` itself in the returned set — unlike a standalone requirements.txt,
    environment.yml's own YAML body is never handed to pip as a file path at
    all (conda extracts and rewrites its `pip:` list into a separate temp
    file first), so there's no sense in which environment.yml's own path is
    itself one of "the files pip reads."
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ManifestParseError(f"{path}: couldn't read ({e})") from e
    _deps, touched = _parse_requirement_lines(
        _environment_yml_pip_requirement_lines(text), path.parent, str(path), set()
    )
    return touched


def _parse_pnpm_workspace_yaml(path: Path) -> list[Dependency]:
    """`pnpm-workspace.yaml` declares no dependencies of its own -- it's a
    workspace-membership file, not a manifest (see `pnpm_workspace_member_names`)
    -- but it still needs a `PARSERS` entry so `find_manifests`/`parse_manifest`
    recognize and return it (as an ordinary, zero-dependency scan target)
    rather than raising "don't know how to parse this file" the moment a
    caller passes it directly, the same way an empty-but-recognized
    manifest of any other format is handled.
    """
    return []


# pip-tools' own convention (https://pip-tools.readthedocs.io/en/stable/#requirementsin-vs-requirementstxt):
# a hand-edited "source" file, `requirements.in`, gets compiled by
# `pip-compile` into the fully-pinned `requirements.txt` this module already
# reads. `pip-compile` parses `requirements.in` with pip's own requirements-
# file parser (piptools.scripts.compile.py builds its InstallRequirements via
# pip._internal.req.req_file.parse_requirements, the exact same entry point
# real pip itself uses for a plain requirements.txt), so it's genuinely the
# identical format -- `-r`/`-c`/`-e`/inline-comment/backslash-continuation
# and all -- just under a different, equally conventional filename. Real,
# current, and common: many pip-tools-managed repos (e.g. jupyterhub,
# home-assistant's dev tooling) commit both files, with `requirements.in`
# the one a human (or an LLM coding assistant) actually edits and therefore
# the one most likely to carry a hallucinated name in the first place.
# Before this fix, neither `PARSERS` nor `find_manifests` recognized this
# filename, and unlike ".txt" there was no suffix-fallback either (see
# `parse_manifest` below) -- so a directory containing only `requirements.in`
# (a real state: reviewing a change before the compiled `.txt` is
# regenerated, or a repo that gitignores the compiled artifact) alongside
# any other supported-but-dependency-free manifest (e.g. a `pyproject.toml`
# that exists purely for `[tool.ruff]`/`[tool.black]` config) reported "0
# dependencies checked, all clean" -- the same silent false-all-clear shape
# already fixed here for Pipfile/setup.cfg/environment.yml, just for this
# still-unhandled pip-tools filename. Confirmed live: `parse_manifest` on a
# `requirements.in` naming a hallucinated package raised "don't know how to
# parse this file" before this fix, and a directory scan alongside a
# config-only pyproject.toml reported a clean 0-dependency scan with exit
# code 0.
PARSERS = {
    "requirements.txt": parse_requirements_txt,
    "requirements.in": parse_requirements_txt,
    "pyproject.toml": parse_pyproject_toml,
    "package.json": parse_package_json,
    "Pipfile": parse_pipfile,
    "setup.cfg": parse_setup_cfg,
    "pnpm-workspace.yaml": _parse_pnpm_workspace_yaml,
    "environment.yml": parse_environment_yml,
    "environment.yaml": parse_environment_yml,
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
        # `pylock.toml`/`pylock.<name>.toml` (see `_PYLOCK_FILENAME_RE`) can't
        # be a literal `PARSERS` key, so it needs its own scan of this
        # directory's actual filenames rather than the dict-key membership
        # check above.
        for filename in filenames:
            if _PYLOCK_FILENAME_RE.match(filename):
                found.append(Path(dirpath) / filename)
    return found


def parse_manifest(path: Path) -> list[Dependency]:
    parser = PARSERS.get(path.name)
    if parser is None and _PYLOCK_FILENAME_RE.match(path.name):
        parser = parse_pylock_toml
    if parser is None and path.suffix in (".txt", ".in"):
        # `find_manifests` only auto-discovers the exact names "requirements.txt"
        # and "requirements.in", but a real pip/pip-tools requirements file is
        # routinely named something else — pip itself doesn't care about the
        # filename at all, only real-world convention does. Home Assistant's
        # core repo splits into requirements_test.txt/requirements_test_pre_commit.txt;
        # cookiecutter-django splits into requirements/base.txt, local.txt,
        # production.txt; a pip-tools project just as commonly splits into
        # requirements/base.in, dev.in the same way (see PARSERS' own
        # "requirements.in" entry above for why `.in` is the identical format).
        # A user pointing slopcheck directly at one of these (a natural thing
        # to do, and exactly how the new `-r`-recursion above resolves nested
        # files too) used to hit `PARSERS[path.name]` -> KeyError, an unhandled
        # traceback instead of a clean error or a real scan. Any other `.txt`/
        # `.in` file is a reasonable enough match for the line-based
        # requirements format (there's no other `.txt`/`.in`-suffixed manifest
        # this tool knows about) to treat the same way rather than crash.
        parser = parse_requirements_txt
    if parser is None:
        raise ManifestParseError(
            f"{path}: don't know how to parse this file "
            "(expected requirements.txt, pyproject.toml, package.json, Pipfile, "
            "setup.cfg, pylock.toml, environment.yml/environment.yaml, or a *.txt/"
            "*.in requirements file)"
        )
    try:
        return parser(path)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError, configparser.Error) as e:
        raise ManifestParseError(f"{path}: couldn't parse as {path.name} ({e})") from e
