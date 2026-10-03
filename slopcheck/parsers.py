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
            names.extend(_hatch_override_deps(env))
    return names


def _hatch_override_deps(env: dict) -> list[str]:
    """Flatten a Hatch environment's `overrides` table into the extra `dependencies`/`extra-dependencies` entries it can inject.

    Hatch's own environment config (https://hatch.pypa.io/latest/config/environment/advanced/#overrides)
    lets `[tool.hatch.envs.<name>]` carry a *third* way to add dependencies,
    on top of the plain `dependencies`/`extra-dependencies` fields
    `_hatch_deps` already reads directly off the env table: an `overrides`
    sub-table of the form `overrides.<source>.<condition>.<option>`, where
    `source` is one of `platform`/`env`/`matrix`/`name` and `option` can
    itself be `dependencies` or `extra-dependencies` (confirmed by reading
    installed Hatch 1.18.1's own `hatch/project/config.py`, which calls
    `apply_overrides(env_name, "matrix", variable, ..., options, ...)` for
    each matrix variable and the analogous calls for `platform`/`env`/`name`
    — all four sources feed the identical option-handling code). Each
    option's value is either a plain list of requirement strings or a list
    of `{value = "...", if = [...]}` tables gating the entry on a specific
    platform/env-var/matrix-value/env-name match (`hatch/project/env.py`'s
    `_apply_override_to_array`).

    Confirmed live and current, not hypothetical (Hatch 1.18.1, a scratch
    project with `[tool.hatch.envs.test] dependencies = ["requests"]`,
    `[[tool.hatch.envs.test.matrix]] pyver = ["a", "b"]`, and
    `[tool.hatch.envs.test.overrides] matrix.pyver.dependencies = [{value =
    "totally-hallucinated-hatch-override-xyz-999", if = ["a"]}]`):
    `hatch env create test.a` genuinely tried (and failed) to resolve the
    fake name from PyPI ("Could not find a version that satisfies the
    requirement totally-hallucinated-hatch-override-xyz-999 (from versions:
    none)"), while `hatch env create test.b` (the matrix variant where the
    `if` condition doesn't match) installed only `requests`, confirming the
    override is genuinely condition-gated at runtime. Before this fix,
    `_hatch_deps` never looked at `overrides` at all, so a hallucinated name
    planted there — invisible in the env's own plain `dependencies` list —
    was silently never checked, the same "0 dependencies checked, all
    clean" false-all-clear shape already fixed here for every other
    unhandled Hatch/PEP 621/PEP 735 field.

    Deliberately does NOT evaluate the `if`/`platform`/`env` gating
    condition itself: slopcheck has no reliable notion of which platform,
    env vars, or matrix variant a real `hatch env create` invocation (by
    this project's own CI, or a contributor's machine) will actually use,
    and the one condition that *did* match in the live check above was
    still a real, installable (if fake) dependency for a real `hatch`
    invocation — so every entry across every source/condition is collected
    unconditionally, the same "safer to over-check than silently miss a
    real hallucination" bias this module already applies elsewhere (e.g.
    the multiple-constraints list form in `_is_poetry_registry_dep`).
    """
    names: list[str] = []
    overrides = env.get("overrides", {})
    if not isinstance(overrides, dict):
        return names
    for source_table in overrides.values():
        if not isinstance(source_table, dict):
            continue
        for condition_table in source_table.values():
            if not isinstance(condition_table, dict):
                continue
            for option in ("dependencies", "extra-dependencies"):
                entries = condition_table.get(option)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, str):
                        names.append(entry)
                    elif isinstance(entry, dict) and isinstance(entry.get("value"), str):
                        names.append(entry["value"])
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
    documented behavior); each referenced file is read through `_file_directive_deps`,
    NOT as a standalone requirements.txt (`parse_requirements_txt`, the real pip
    requirements-file parser, used to be called here directly) — see
    `_file_directive_deps`'s own docstring for why that's the wrong reference
    implementation for a `file:`-style directive: setuptools' own file-reading
    code never gives `-r`/`-i`/`-e`/`-c` any special meaning, and a `-r` line in
    particular makes a real build Fatal immediately instead of being followed.
    """
    dynamic_fields = set(data.get("project", {}).get("dynamic", []))
    setuptools_dynamic = data.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    deps: list[Dependency] = []

    if "dependencies" in dynamic_fields:
        for filename in _setuptools_dynamic_files(setuptools_dynamic.get("dependencies")):
            deps.extend(_file_directive_deps(base_dir / filename))

    if "optional-dependencies" in dynamic_fields:
        for group_spec in setuptools_dynamic.get("optional-dependencies", {}).values():
            for filename in _setuptools_dynamic_files(group_spec):
                deps.extend(_file_directive_deps(base_dir / filename))

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


# Pipenv's own "category" feature (`pipenv install --categories foo`,
# https://pipenv.pypa.io/en/latest/indexes/#specifying-package-categories)
# lets a Pipfile declare dependency groups under ANY top-level table name,
# not just the conventional `[packages]`/`[dev-packages]` pair. Reading
# Pipenv 2026.8.0's own source directly (`pipenv/utils/pipfile.py`'s
# `Pipfile.get_package_categories`) shows it treats every top-level Pipfile
# section as a package category *except* a fixed, explicit exclusion list —
# its own `NON_CATEGORY_SECTIONS` constant, mirrored here verbatim — not an
# allowlist of conventional names. Confirmed live (real Pipenv 2026.8.0): a
# Pipfile with an arbitrary `[feature-x-packages]` table (no special meaning
# to Pipenv beyond "not one of the excluded names") containing a
# hallucinated package name made a real `pipenv lock` genuinely try and fail
# to resolve it from PyPI ("Could not find a version that satisfies the
# requirement ... (from versions: none)") -- the identical failure shape as
# a name sitting in `[packages]`, with no `--categories` flag needed; lock
# generation processes every category by default. Before this fix,
# `parse_pipfile` only ever read the two literal section names "packages"/
# "dev-packages", so any custom category's dependencies -- a real,
# documented, current Pipenv feature for splitting optional/feature-scoped
# dependency groups -- were silently never checked at all, the same silent
# false-all-clear shape this module's other manifest-coverage fixes already
# closed for entirely different file formats.
_PIPFILE_NON_CATEGORY_SECTIONS = frozenset({"build-system", "pipenv", "requires", "scripts", "source"})


def _pipfile_category_sections(data: dict) -> list[str]:
    """Every top-level Pipfile table Pipenv itself treats as a package category.

    Shared with `private_registry.pipfile_private_registry_context`, which
    needs the identical category list to scope a per-dependency `index=` key
    (Pipenv's private-source pinning) correctly across every category, not
    just `packages`/`dev-packages`.
    """
    return [
        section
        for section, value in data.items()
        if section not in _PIPFILE_NON_CATEGORY_SECTIONS and isinstance(value, dict)
    ]


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
    so it's parsed the same way as `pyproject.toml`. Every category section
    `_pipfile_category_sections` recognizes is read (see its own docstring
    for why that's not just `[packages]`/`[dev-packages]`); `[requires]`
    (Python version) and `[[source]]` (Pipenv's own private-index config,
    analogous to Poetry's `[[tool.poetry.source]]`) aren't dependency names.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    names = []
    for section in _pipfile_category_sections(data):
        names.extend(_pipfile_deps(data, section))
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
    downloadable *file*, but per PEP 751's own schema each of those three
    sub-tables resolves that file via *either* a `url` key *or* a `path` key
    (the spec: "If a relative path is used it MUST be relative to the
    location of this file") — `path` is a local filesystem reference, not a
    downloadable one, used for a package whose actual file lives on disk
    next to the lock file rather than on an index.

    Confirmed live (uv 0.12.19, no mocking): built a real wheel for a
    never-published package, referenced it from `[tool.uv.sources]` via a
    *file* path (`{ path = "../dist/<name>-0.1.0-py3-none-any.whl" }` —
    distinct from `directory`, which points at a source *tree*), then ran
    `uv export --format pylock.toml`. uv never issued a single PyPI request
    for that name (there's nothing to resolve — the exact file is already
    named) and wrote `[[packages]] archive = { path = "../dist/...", hashes
    = {...} }` with no `url` key anywhere in the entry at all. Before this
    fix, `_is_pylock_registry_package` only excluded `vcs`/`directory`, so
    this shape — archive/sdist/wheels present but url-less — was still sent
    to the PyPI existence check and flagged `not_found`, a false
    "hallucinated" positive on a legitimately local-only package the real
    locker tool never queried the registry for, the identical bug shape
    already fixed for Poetry's/uv's/Pipfile's git/path dependency tables,
    just for pylock.toml's own, independently-shaped archive/sdist/wheels
    url-vs-path split.

    A package is still registry-resolved if *any* of its archive/sdist/
    wheels sub-tables carries a `url` (mirrors this module's existing
    multiple-constraints bias, e.g. `_is_poetry_registry_dep`'s list form:
    treat a name as still-checkable if any one of its sources is
    registry-backed) — a real locker only ever omits `url` from *every*
    file for a genuinely local-only resolution; a mixed index/local
    reference isn't a shape any real locker produces, but erring toward
    "still check it" for that theoretical mix avoids a false negative.
    """
    if any(key in package for key in ("vcs", "directory")):
        return False
    sources = [package.get("archive"), package.get("sdist"), *package.get("wheels", [])]
    sources = [s for s in sources if isinstance(s, dict)]
    if not sources:
        return True
    return any("url" in s for s in sources)


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

# A real pip/pip-tools requirements file routinely isn't named exactly
# "requirements.txt"/"requirements.in" -- see `find_manifests`' own docstring
# for the two live, currently-real examples (home-assistant/core's
# "requirements_test.txt", cookiecutter-django's "requirements/base.txt")
# this closes. Deliberately narrower than "every *.txt/*.in file": a bare
# suffix match would also walk into any unrelated prose ".txt" file a repo
# happens to contain (a CHANGELOG, a wordlist fixture, ...) and feed it
# through the line-based requirement regex, which is permissive enough to
# treat a lone single-token line as a "dependency" name -- a real, if
# contrived, false-positive-flood risk a plain suffix check doesn't have.
# Requiring "requirement" (case-insensitive) in either the filename itself
# or its immediate parent directory name covers both real examples above
# without widening the net to arbitrary text files.
_REQUIREMENTS_FILENAME_HINT_RE = re.compile(r"requirement", re.IGNORECASE)


def _setup_cfg_drop_comment(line: str) -> str:
    """Drop a setup.cfg list value's trailing comment the way setuptools itself does.

    `_strip_inline_comment`/`_INLINE_COMMENT_RE` above correctly mirrors real
    pip's own requirements.txt comment rule (`req_file.COMMENT_RE = r"(^|\\s+)
    #.*$"`, confirmed by reading pip 25.x's own source: *any* run of
    whitespace before `#` starts a comment) — but setuptools' own
    `install_requires`/`[options.extras_require]`/`setup_requires` list-value
    preprocessing is a completely different pipeline that happens to look
    similar: `setuptools._reqs.parse_strings` maps `jaraco.text.drop_comment`
    over each already-stripped line before ever constructing a
    `packaging.requirements.Requirement` from it, and `drop_comment`'s own
    implementation (confirmed by reading jaraco.text 4.0.0's vendored source
    directly — the exact copy setuptools 84.0.0 vendors) is just
    `line.partition(' #')[0]`: a single literal space immediately before `#`,
    not pip's broader "any whitespace" rule. A trailing comment preceded by a
    tab (a real, plausible shape — tab-aligning trailing comments is an
    ordinary editor/style habit, and nothing about setup.cfg's INI syntax
    discourages it) doesn't match `' #'` at all, so the "comment" — `#` and
    all — stays glued onto the requirement string in real setuptools, headed
    straight for `packaging.requirements.Requirement()`.

    Confirmed live (setuptools 84.0.0, real `python -m build --sdist` against
    a from-scratch `setup.cfg` with `install_requires =\\n\\trequests\\t#
    http client\\n\\ttotally-hallucinated-package-xyz-456`): the build
    Fataled immediately — `packaging.requirements.InvalidRequirement:
    Expected semicolon (after name with no version specifier) or end`,
    pointing at the tab-separated comment — before resolving a single
    dependency, real or hallucinated. Before this fix, `_setup_cfg_list_deps`
    reused `_strip_inline_comment` here, which treats a tab exactly like a
    space (both match `\\s`) and silently stripped the tab-preceded comment,
    reporting the pre-comment name ("requests") as an ordinary, checkable
    dependency and implying this setup.cfg installs cleanly — when a real
    `pip install .`/`python -m build` genuinely crashes on it before any
    dependency, including a hallucinated one sitting on a syntactically fine
    neighboring line, is ever resolved. The same "real tool never gets this
    far, so reporting ok/not_found here is misleading" shape already fixed
    for this module's `file:`-directive handling, just for a different
    syntax trap in the same underlying setuptools requirement pipeline.
    """
    return line.partition(" #")[0]


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
        line = _setup_cfg_drop_comment(line)
        if not line or "://" in line:
            continue
        match = _REQ_LINE_RE.match(line)
        if match:
            deps.append(Dependency(match.group("name"), "pypi", str(path)))
    return deps


def _file_directive_deps(file_path: Path) -> list[Dependency]:
    """Read a setuptools `file:`-referenced file the way setuptools itself does:
    as a flat, non-recursive, pip-directive-blind text blob — NOT a real pip
    requirements file.

    Both `_setup_cfg_requirements_value` (setup.cfg's `install_requires`/
    `[options.extras_require]`) and `_setuptools_dynamic_deps` (PEP 621's
    `[tool.setuptools.dynamic]` equivalent) used to read a `file:`-referenced
    file with `parse_requirements_txt` — the real pip requirements-file
    parser, which recognizes and follows `-r`/`-c`, and silently skips `-e`/
    URL lines as directives rather than names. That's the wrong reference
    implementation for this call site: setuptools' own file-reading code
    (confirmed reading setuptools 84.0.0's `setupcfg.py` `_parse_file`/
    `_parse_requirements_list` and `pyprojecttoml.py` `_expand_directive`,
    both of which bottom out in the identical `expand.read_files` + a plain
    `_parse_list_semicolon` split) never gives any of pip's own command-line
    directives special meaning at all — every non-blank, non-`#`-comment
    line becomes a literal candidate requirement string, full stop. There is
    no `-r`-recursion step, and a `-i`/`-e`/`-c` line isn't skipped, it's kept
    verbatim and handed straight to `packaging.requirements.Requirement()`
    during the real build.

    Live-verified against real setuptools 84.0.0 (this project's own pinned
    minimum) and real pip: a `file:`-target containing
    `-r requirements-common.txt` (a real, easy-to-write pattern when the same
    physical `requirements.txt` also gets used directly via `pip install -r`,
    where `-r` genuinely is followed) makes `read_configuration`'s resolved
    `install_requires` literally include the string
    `'-r requirements-common.txt'` — and a real `pip install .`/
    `python -m build` against that setup.cfg Fatals immediately with
    `packaging.requirements.InvalidRequirement: Expected package name at the
    start of dependency specifier`, before a single dependency (including an
    innocent sibling requirement on the next line) is ever resolved. The
    identical crash reproduces for a PEP 621 `[tool.setuptools.dynamic]
    dependencies = {file = [...]}` pointing at the same kind of file — both
    code paths bottom out in the same `Distribution._normalize_requires`
    call. Before this fix, `parse_requirements_txt`'s own `-r`-following
    behavior made slopcheck recurse into `requirements-common.txt` and report
    whatever hallucinated name sat there as a plain `not_found` dependency of
    the project — actively misleading, since the real tool never gets far
    enough to resolve (or even attempt to resolve) that name at all; the real,
    actionable problem (the malformed `-r` line itself, which breaks the
    build outright) was never mentioned.

    Uses `_setup_cfg_list_deps` (the same splitter already used for a plain,
    non-`file:` setup.cfg list value, which matches real setuptools'
    `_parse_list_semicolon` exactly: split on newline if present, else `;`,
    drop blank lines, regex-match each remaining chunk as a name) rather than
    `parse_requirements_txt`, so a `-r`/`-i`/`-e`/url line is treated exactly
    like real setuptools treats it: not specially recognized, and — since it
    doesn't match a plain package name — simply never extracted as a
    dependency, the same as any other line this simple parser can't make
    sense of.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _setup_cfg_list_deps(text, file_path)


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
    `dynamic` gaps already fixed here. Reads the referenced file through
    `_file_directive_deps` (NOT `parse_requirements_txt` — see that
    function's own docstring for why the real pip requirements-file parser
    is the wrong reference implementation here), so `Dependency.source`
    correctly points at the file the name was actually found in.

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
            deps.extend(_file_directive_deps(base_dir / filename))
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
    duplicating its logic.

    The claim above ("only `[tool.poetry.dependencies]` is read here —
    `[tool.poetry.group.*.dependencies]` only ever enrich the *legacy*
    declaration path") was wrong: `Factory._configure_package_dependency_groups`
    (same poetry-core file, read directly) does the identical by-name merge
    for PEP 735 `[dependency-groups]` whenever a `[tool.poetry.group.<name>]`
    table shares that group's name — `DependencyGroup.dependencies_for_locking`
    folds `_poetry_dependencies` (populated from `[tool.poetry.group.<name>.
    dependencies]` via `_add_package_poetry_group_dependencies`) into
    `_dependencies` (populated from `[dependency-groups].<name>` via
    `_add_package_pep735_group_dependencies`) by matching dependency name,
    exactly mirroring how `[tool.poetry.dependencies]` enriches the MAIN
    group's `[project.dependencies]` entries.

    Live-verified against real Poetry 2.5.1 (`poetry lock -vvv`): a scratch
    pyproject.toml with `[dependency-groups] test =
    ["totally-hallucinated-slopcheck-group-test-xyz-999"]` plus
    `[tool.poetry.group.test.dependencies] totally-hallucinated-slopcheck-
    group-test-xyz-999 = {git = "file:///tmp/fakepkg"}` (a local git repo
    whose own pyproject.toml declares that exact name, confirmed to 404 on
    the real public PyPI JSON API) made `poetry lock` clone the git repo and
    resolve the dependency with zero requests to pypi.org — `poetry.lock`
    records it as a plain git-sourced package. Before this fix,
    `_dependency_groups_deps` (PEP 735) still emitted the bare name as a raw
    spec, nothing excluded it (this function only ever read
    `[tool.poetry.dependencies]`, never `[tool.poetry.group.*]`), and
    `slopcheck` reported `not_found` for a dependency a real `poetry lock`/
    `poetry install --with test` never touches PyPI for at all — the same
    false-hallucination-on-a-legitimately-git-sourced-name shape the MAIN
    group fix above was built to close, just for the dependency-groups/
    poetry-group pairing instead of the project-dependencies/tool-poetry
    pairing.

    Deliberately not scoped to only groups whose name also appears in
    `[dependency-groups]`: a `[tool.poetry.group.<name>.dependencies]` table
    with no matching `[dependency-groups]` entry (pure legacy Poetry-group
    usage) never gets its names emitted into `raw_specs` by any other
    reader either, so including its git/path/url-sourced names in this
    skip-set unconditionally is a no-op for that case and correct for the
    PEP-735-overlap case — the same whole-table-not-precise-overlap
    conservatism `pdm_private_registry_context`'s own docstring already
    accepts for a structurally similar scoping question.
    """
    poetry = data.get("tool", {}).get("poetry", {})
    names = {
        _normalize_name(name)
        for name, spec in poetry.get("dependencies", {}).items()
        if not _is_poetry_registry_dep(spec)
    }
    for group in poetry.get("group", {}).values():
        if not isinstance(group, dict):
            continue
        names |= {
            _normalize_name(name)
            for name, spec in group.get("dependencies", {}).items()
            if not _is_poetry_registry_dep(spec)
        }
    return names


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
#
# "exec:" is Yarn Berry's own protocol (https://yarnpkg.com/features/protocols#exec)
# for building a package on the fly from a local script instead of fetching
# it from any registry — real, current usage (Yarn's own docs example:
# generating a virtual package from a build step) writes a descriptor that's
# just a relative path to that script, with no requirement that the path
# contain a "/" at all when the script sits next to package.json itself (a
# bare filename, e.g. "exec:builder.js", is syntactically valid — unlike the
# git-host shorthand forms above, there's no slash-bearing fallback to catch
# this one). Confirmed live (Yarn Berry 4.18.1, `yarn install`, scripts
# enabled): a scratch package.json with `"totally-hallucinated-execprotocol-
# xyz-556": "exec:builder.js"` (a sibling `builder.js` present) failed
# resolution with `totally-hallucinated-execprotocol-xyz-556@exec:builder.js
# ...: Manifest not found` — a purely local-filesystem error raised during
# the Resolution step, before Yarn's own install even reaches its Fetch step
# — and made zero requests to `registry.yarnpkg.com`/`registry.npmjs.org`
# for that name. Before this fix, `_npm_non_registry_version("exec:builder.js")`
# returned `False` (no recognized prefix, no "/", doesn't start with "." or
# a drive letter), so slopcheck sent the name to the public registry and
# would report it a plain `not_found` hallucination — a false positive on a
# dependency a real `yarn install` never asks the registry about at all.
_NON_REGISTRY_PREFIXES = (
    "workspace:",
    "file:",
    "link:",
    "portal:",
    "git:",
    "git+",
    "github:",
    "gist:",
    "exec:",
)

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

    A string value can also be a `file:`/git-URL/tarball-URL specifier
    (npm's own docs: overrides values accept "an exact version, a semver
    range, a dist-tag, or a replacement specifier such as npm:, file:, or
    a Git URL") — confirmed live (npm 12.2.0): `{"wrappy": "file:./local-
    fork"}` makes `npm install --dry-run -v` fetch only the *other* direct
    dependency from the registry, never `wrappy`, which is copied straight
    from the path instead. The key's name never reaches the registry in
    that case, so it must be skipped the same way a non-registry
    `dependencies` value already is, not sent in as a plain name to check.
    """
    names = []
    for name, value in overrides.items():
        if name == ".":
            continue
        if isinstance(value, str):
            if value.startswith(_NPM_ALIAS_PREFIX):
                names.append(_npm_alias_target(value))
            elif not _npm_non_registry_version(value):
                names.append(_strip_version_suffix(name))
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


def _workspace_patterns_include_dir(root: Path, patterns: list[str], target: Path) -> bool:
    """True if one of `patterns` (rooted at `root`) resolves to exactly `target`."""
    for pattern in patterns:
        glob_pattern = pattern.removeprefix("!")
        try:
            matches = root.glob(glob_pattern)
        except (ValueError, NotImplementedError):
            continue
        for member_dir in matches:
            try:
                if member_dir.resolve() == target:
                    return True
            except OSError:
                continue
    return False


def npm_workspace_root(start: Path) -> Path | None:
    """Find the nearest ancestor directory whose package.json claims `start` as a workspace member.

    npm resolves its own per-project `.npmrc` relative to the *workspace
    root*, not necessarily the cwd's own package.json directory, once `start`
    sits inside an npm (or Yarn Classic) workspace — confirmed live (npm
    9.2.0): a two-level layout (`root/package.json` with `"workspaces":
    ["packages/*"]` plus `root/.npmrc` carrying a `@acmetest:registry=...`
    scope mapping, and `root/packages/foo/package.json` with *no* `.npmrc`
    of its own) ran `npm install --loglevel verbose` from inside
    `packages/foo` and the debug log showed `info found workspace root at
    .../root` followed by `config:load:project` loading *that* directory's
    `.npmrc` — genuinely applying the root's scope mapping to a dependency
    declared only in the member's own package.json. A plain nested
    package.json with no enclosing `workspaces` field does *not* get this
    treatment (confirmed live the same way): npm then treats the nested
    package.json as its own project root and never reads the parent
    directory's `.npmrc` at all, so this only returns an ancestor whose
    `workspaces` patterns actually resolve to `start` as a member -- not
    just the nearest ancestor containing a package.json.

    Before this fix, slopcheck's private-registry detection only ever
    checked `.npmrc` in the exact directory holding each *scanned*
    package.json (see `_npmrc_paths`). Scanning a workspace member directory
    on its own -- a realistic shape, e.g. a CI job or pre-commit hook scoped
    to one changed package -- never saw the workspace root's `.npmrc` at
    all (it isn't even among the scanned manifests), so a dependency a real
    `npm install` would resolve against the configured private registry was
    misreported as a plain `not_found` hallucination instead of downgraded
    to `private`.
    """
    start_resolved = start.resolve()
    for ancestor in start_resolved.parents:
        pkg = ancestor / "package.json"
        if not pkg.is_file():
            continue
        try:
            data = json.loads(pkg.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        patterns = _npm_workspace_patterns(data)
        if not patterns:
            continue
        if _workspace_patterns_include_dir(ancestor, patterns, start_resolved):
            return ancestor
    return None


def _uv_workspace_patterns(data: dict) -> tuple[list[str], list[str]]:
    """Extract (members, exclude) glob patterns from a pyproject.toml's `[tool.uv.workspace]`."""
    workspace = data.get("tool", {}).get("uv", {}).get("workspace", {})
    if not isinstance(workspace, dict):
        return [], []
    members = [m for m in workspace.get("members", []) if isinstance(m, str)]
    exclude = [m for m in workspace.get("exclude", []) if isinstance(m, str)]
    return members, exclude


def uv_workspace_root(start: Path) -> Path | None:
    """Find the nearest ancestor pyproject.toml whose `[tool.uv.workspace]` claims `start` as a member.

    uv (https://docs.astral.sh/uv/concepts/projects/workspaces/) resolves a
    workspace member's package index config from the *workspace root*'s
    `pyproject.toml`/`uv.toml`, not necessarily the member's own directory —
    confirmed live (uv 0.12.19): a two-level layout (`root/pyproject.toml`
    with `[tool.uv.workspace] members = ["pkgs/*"]` plus a `[[tool.uv.index]]`
    entry pointing at an unreachable `http://127.0.0.1:9/simple`, and
    `root/pkgs/foo/pyproject.toml` with *no* uv config of its own, naming a
    fake dependency) made `uv lock` run from *inside* `pkgs/foo` genuinely
    discover the workspace root ("Found static `pyproject.toml` for: ws-root
    @ file:///.../root") and attempt to resolve the fake dependency against
    that root-configured private index (`Connection refused` to
    `127.0.0.1:9`, never contacted PyPI) — the same single-shared-lockfile
    behavior as a `uv lock`/`uv sync` run from the root itself.

    Before this fix, slopcheck's uv private-registry detection
    (`uv_private_registry_context`) only ever looked at `uv.toml`/
    `[tool.uv]` in the directory holding each *scanned* pyproject.toml.
    Scanning a workspace member directory on its own — a realistic shape,
    e.g. a monorepo CI job or pre-commit hook scoped to one changed package,
    the exact same pattern already fixed for npm/Yarn workspaces via
    `npm_workspace_root` — never saw the workspace root's index config at
    all (it isn't even among the scanned manifests), so a dependency a real
    `uv lock`/`uv sync` would resolve against the configured private index
    was misreported as a plain `not_found` hallucination instead of
    downgraded to `private`.

    A plain nested pyproject.toml with no enclosing `[tool.uv.workspace]`
    does *not* get this treatment: this only returns an ancestor whose
    `members` patterns actually resolve to `start`, mirroring
    `npm_workspace_root`'s exact-member-match rule rather than just the
    nearest ancestor containing a pyproject.toml. An `exclude` pattern
    matching `start` (uv's own documented way to carve a directory back out
    of an otherwise-matching `members` glob) takes precedence, the same way
    real uv's own workspace discovery treats it.
    """
    start_resolved = start.resolve()
    for ancestor in start_resolved.parents:
        pyproject = ancestor / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        members, exclude = _uv_workspace_patterns(data)
        if not members:
            continue
        if exclude and _workspace_patterns_include_dir(ancestor, exclude, start_resolved):
            continue
        if _workspace_patterns_include_dir(ancestor, members, start_resolved):
            return ancestor
    return None


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


def pnpm_workspace_root(start: Path) -> Path | None:
    """Find the nearest ancestor directory whose `pnpm-workspace.yaml` claims `start` as a member.

    The pnpm analog of `npm_workspace_root`/`uv_workspace_root`/
    `pdm_workspace_root`: pnpm declares workspace membership in a sibling
    file at the workspace *root* (`pnpm-workspace.yaml`), never inside a
    member's own directory, so a member scanned on its own has no way to
    see it unless something walks up looking for it -- exactly the gap
    already closed for npm/Yarn Classic, uv, and PDM's own, differently-
    shaped workspace mechanisms, just never ported to pnpm's.

    Confirmed live (pnpm 12.8.1, a from-scratch two-package workspace: root
    `pnpm-workspace.yaml` with `packages: ['packages/*']` and
    `linkWorkspacePackages: true` -- pnpm's own current, documented setting
    for resolving an ordinary-semver-range dependency on a workspace
    sibling locally instead of from the registry; as of this pnpm version
    it's read from `pnpm-workspace.yaml` itself, not a root `.npmrc` the
    way an older pnpm accepted -- and member `packages/app` depending on
    sibling `packages/internal-lib` with a plain `"^1.0.0"` range, no
    `workspace:` protocol anywhere): running `pnpm install` from inside
    `packages/app` itself resolved the sibling entirely locally (a real
    symlink into `node_modules/@scratch/internal-lib`, confirmed via a
    `--loglevel debug` trace with zero `registry.npmjs.org` requests for
    that name) -- pnpm discovers the workspace root by walking up from the
    current directory looking for `pnpm-workspace.yaml`, exactly mirroring
    how `pdm_workspace_root`'s own docstring describes PDM doing the same
    for its own workspace file. Before this fix, `slopcheck`, scanning only
    `packages/app` (a realistic shape -- a monorepo CI job or pre-commit
    hook scoped to one changed package, the exact pattern already fixed for
    npm/uv/PDM), never saw the root's `pnpm-workspace.yaml` at all -- it
    isn't even among the scanned manifests -- so the sibling's name was
    reported a plain `not_found` hallucination instead of recognized as
    locally-resolved; scanning the whole tree (so the root's
    `pnpm-workspace.yaml` was among `paths` too) already worked correctly.

    A plain nested `package.json` with no ancestor `pnpm-workspace.yaml`
    whose `packages` glob actually resolves to `start` does not get this
    treatment, mirroring every sibling `*_workspace_root` function's
    identical exact-member-match rule rather than just the nearest ancestor
    containing the workspace file.
    """
    start_resolved = start.resolve()
    for ancestor in start_resolved.parents:
        workspace_file = ancestor / "pnpm-workspace.yaml"
        if not workspace_file.is_file():
            continue
        try:
            text = workspace_file.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        patterns = _pnpm_workspace_patterns(text)
        if not patterns:
            continue
        if _workspace_patterns_include_dir(ancestor, patterns, start_resolved):
            return ancestor
    return None


def pnpm_workspace_member_names(
    pnpm_workspace_paths: list[Path], package_json_paths: list[Path] | None = None
) -> list[tuple[Path, set[str]]]:
    """For every `pnpm-workspace.yaml` found (directly, or discovered as a scanned package.json's workspace root), the local package names it resolves without the registry.

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

    `package_json_paths` (optional, the scanned `package.json` files — e.g.
    `cli.scan`'s own `paths`) lets this also discover a workspace root that
    was never itself among the scanned paths at all: scanning only a
    workspace *member* directory (the same realistic monorepo-CI/pre-commit
    shape already fixed for npm via `npm_workspace_root`, uv via
    `uv_workspace_root`, and PDM via `pdm_workspace_root`) never includes
    the root's own `pnpm-workspace.yaml` in `pnpm_workspace_paths` — it
    isn't even among the scanned manifests — so without this, a sibling
    member's name was invisible no matter what. See `pnpm_workspace_root`'s
    own docstring for the live-verified repro this closes. A root
    discovered this way is folded in alongside any `pnpm-workspace.yaml`
    passed directly, de-duplicated, so scanning the whole tree (where the
    root is already in `pnpm_workspace_paths`) is unaffected.
    """
    all_paths = list(pnpm_workspace_paths)
    if package_json_paths:
        project_roots = [p.parent for p in package_json_paths]
        discovered_roots = {root for root in (pnpm_workspace_root(r) for r in project_roots) if root is not None}
        all_paths.extend(root / "pnpm-workspace.yaml" for root in discovered_roots)
    all_paths = list(dict.fromkeys(all_paths))

    pairs: list[tuple[Path, set[str]]] = []
    for path in all_paths:
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


def pdm_workspace_root(start: Path) -> Path | None:
    """Find the nearest ancestor pyproject.toml whose `[tool.pdm.workspace]` claims `start` as a member.

    The PDM analog of `uv_workspace_root`/`npm_workspace_root`: PDM's own
    `WorkspaceManager.project` (`pdm/project/workspace.py`) walks `start`'s
    ancestors looking for the nearest one whose pyproject.toml both declares
    `[tool.pdm.workspace]` (any `members` list at all -- `is_root` only checks
    truthiness) *and* whose resolved `members` glob actually includes `start`
    -- confirmed by reading that function directly, which is exactly what a
    real `pdm lock`/`pdm install` consults to decide "is the directory I'm
    scanning part of a workspace, and if so, which root owns it."

    Before this fix, both `pdm_private_registry_context` and
    `pdm_workspace_member_names` only ever looked at the directory holding
    each *scanned* pyproject.toml. Scanning a workspace member directory on
    its own -- a realistic shape, e.g. a monorepo CI job or pre-commit hook
    scoped to one changed package, the exact pattern already fixed for
    uv via `uv_workspace_root` -- never saw the workspace root's
    `[[tool.pdm.source]]`/`pdm.toml`/`config.toml` private-index config, nor
    the root's own `[tool.pdm.workspace].members` list naming the member's
    *siblings* (so a dependency on a sibling workspace member, resolved
    entirely locally by a real `pdm lock` run from the root -- the only way
    PDM allows it to run at all, confirmed live: `pdm lock`/`pdm install`
    both hard-error with "can only be run from the workspace root" when
    invoked from inside a member directory -- was reported a plain
    `not_found` hallucination instead of recognized as a local package).

    Live-verified (PDM 2.29.2): a two-member workspace (root
    `[tool.pdm.workspace] members = ["packages/*"]`, member `packages/member`
    depending on sibling-member name `packages/other` declares, zero
    `[[tool.pdm.source]]` anywhere) resolved the dependency entirely locally
    (`unearth.preparer: The file packages/other is a local directory, use it
    directly`) when `pdm lock` ran from the root -- the only invocation PDM
    permits. `slopcheck`, scanning `packages/member` alone, reported the
    sibling name `not_found`; scanning the whole tree (so both pyproject.toml
    files were in `paths`) correctly reported nothing. Separately, the same
    workspace with a root-level `[[tool.pdm.source]]` pointed at an
    unreachable address made `pdm lock` (again, from the root) genuinely
    attempt that address for the *member's* own dependency
    (`pdm.termui: Adding requirement <name>(from member 0.1.0)`, then a
    `ConnectError` to the configured private source) -- `slopcheck` scanning
    the member alone reported `not_found` where scanning the whole tree
    correctly reported `private`.
    """
    start_resolved = start.resolve()
    for ancestor in start_resolved.parents:
        pyproject = ancestor / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        members = _pdm_workspace_patterns(data)
        if not members:
            continue
        if _workspace_patterns_include_dir(ancestor, members, start_resolved):
            return ancestor
    return None


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

    A scanned pyproject.toml that's actually a workspace *member* (not the
    workspace root itself) pulls in its enclosing root via
    `pdm_workspace_root` first — see that function's own docstring for the
    gap this closes (the PDM analog of `uv_workspace_root`'s identical fix
    for uv). Without this, scanning just a member directory never saw the
    root's `[tool.pdm.workspace]` at all, so it could never recognize any
    of the member's siblings — including itself, if a cousin member
    happened to depend on it — as locally-resolved.
    """
    project_roots = [p.parent for p in pyproject_paths]
    workspace_roots = {root for root in (pdm_workspace_root(r) for r in project_roots) if root is not None}
    all_pyproject_paths = list(
        dict.fromkeys([*pyproject_paths, *(root / "pyproject.toml" for root in workspace_roots)])
    )

    pairs: list[tuple[Path, set[str]]] = []
    for path in all_pyproject_paths:
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
    # A value can also be a `file:`/git-URL/tarball-URL specifier instead of
    # a version/range/npm: alias — confirmed live (Yarn Classic 1.22.22):
    # `{"wrappy": "file:./local-fork"}` resolves `wrappy` straight from the
    # path, never querying registry.yarnpkg.com for it at all. The pattern's
    # name never reaches the registry in that case, so it's skipped the same
    # way a non-registry `dependencies`/`overrides` value already is.
    for pattern, value in data.get("resolutions", {}).items():
        if isinstance(value, str) and value.startswith(_NPM_ALIAS_PREFIX):
            deps.append(Dependency(_npm_alias_target(value), "npm", str(path)))
        elif not (isinstance(value, str) and _npm_non_registry_version(value)):
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


_PNPM_WORKSPACE_OVERRIDE_ENTRY_RE = re.compile(r"^([^:\s][^:]*):\s*(.*)$")


def _pnpm_workspace_overrides(text: str) -> dict[str, str]:
    """Extract pnpm-workspace.yaml's own top-level `overrides:` mapping (key -> raw value string).

    pnpm's `overrides` field (https://pnpm.io/settings/dependency-resolution#overrides)
    can be set at the root of the project either in `package.json`'s `pnpm.overrides`
    (already read by `_npm_overrides_deps`/`parse_package_json`) or, as of a real,
    current pnpm feature, directly in `pnpm-workspace.yaml` instead -- pnpm's own docs
    show the identical field under a `pnpm-workspace.yaml:` heading. Not a general YAML
    parser, the same "hand-parse the subset of syntax that matters" approach already
    used for this same file's own `packages:` list (`_pnpm_workspace_patterns`): a real
    `overrides:` block is always a flat mapping of quoted-or-bare string keys to
    quoted-or-bare string values, one level of indentation deep, confirmed against
    pnpm's own docs examples and real `pnpm-workspace.yaml` files it generates.
    """
    overrides: dict[str, str] = {}
    in_overrides = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            top_match = _PNPM_WORKSPACE_TOP_KEY_RE.match(stripped)
            in_overrides = bool(top_match) and top_match.group(1) == "overrides" and not top_match.group(2)
            continue
        if not in_overrides:
            continue
        entry_match = _PNPM_WORKSPACE_OVERRIDE_ENTRY_RE.match(stripped)
        if entry_match:
            key = entry_match.group(1).strip().strip("\"'")
            value = entry_match.group(2).strip().strip("\"'")
            if key:
                overrides[key] = value
    return overrides


def _pnpm_workspace_override_names(overrides: dict[str, str]) -> list[str]:
    """Resolve a pnpm-workspace.yaml `overrides` mapping into the npm package names it actually fetches.

    Confirmed live (pnpm 12.8.1): an override key is a package selector, optionally
    scoped to a specific parent with pnpm's own `"parent@version>dependency"` syntax
    (https://pnpm.io/settings/dependency-resolution#overrides) -- only the segment
    *after* the last `>` is the real dependency name being overridden, the same way
    `>`-free keys (`"foo"`, `"bar@^2.1.0"`) already need their own trailing `@version`
    pin stripped. A plain version-string value (including a `catalog:`/`catalog:<name>`
    reference, which only redirects to a centrally-managed version and names no new
    package) doesn't change which name pnpm resolves -- the key's own (derived) name is
    what gets checked, exactly as `_npm_overrides_deps` already does for package.json's
    sibling `overrides`/`pnpm.overrides` fields. An `npm:`-prefixed value substitutes a
    *different* real package for the key's name instead (same aliasing rule
    `_npm_overrides_deps` already handles) -- confirmed live: `overrides: {is-number:
    'npm:totally-hallucinated-pnpm-ws-override-alias-xyz-321@1.0.0'}` (with `is-number`
    a real, already-resolved transitive dependency, named nowhere in any package.json)
    made `pnpm install` genuinely issue `GET https://registry.npmjs.org/
    totally-hallucinated-pnpm-ws-override-alias-xyz-321` and fail with a real 404 --
    `is-number` itself was never fetched under its own name at all once overridden. A
    literal `"-"` value (pnpm's own documented syntax for removing a dependency
    entirely rather than overriding its version) is skipped outright: real pnpm never
    fetches a removed dependency, so checking the key's name here would be checking
    something pnpm has deliberately made irrelevant to this install, not a hallucination
    risk. A value can likewise be a `file:`/git-URL/tarball-URL specifier -- confirmed
    live (pnpm 12.8.1): `overrides: {wrappy: 'http://127.0.0.1:8911/wrappy-1.0.2.tgz'}`
    resolves `wrappy` purely by URL, with zero requests to registry.npmjs.org for it --
    same as `_npm_overrides_deps` and the Yarn `resolutions` reader, this must be skipped
    rather than checked under the key's own name.

    Before this fix, `_parse_pnpm_workspace_yaml` discarded a `pnpm-workspace.yaml`'s
    entire body unconditionally -- it existed only so `find_manifests`/`parse_manifest`
    would recognize the filename as an ordinary, zero-dependency scan target, correct
    for the file's *workspace-membership* role (`packages:`, read separately by
    `pnpm_workspace_member_names`) but silently wrong for this entirely different
    field living in the same file: a real, install-breaking hallucinated override
    target was invisible to slopcheck no matter what, since neither this function nor
    any package.json reader ever looked at it.
    """
    names = []
    for key, value in overrides.items():
        if value == "-":
            continue
        if value.startswith(_NPM_ALIAS_PREFIX):
            names.append(_npm_alias_target(value))
        elif not _npm_non_registry_version(value):
            names.append(_strip_version_suffix(key.rsplit(">", 1)[-1]))
    return names


def _parse_pnpm_workspace_yaml(path: Path) -> list[Dependency]:
    """`pnpm-workspace.yaml` is primarily a workspace-membership file (see
    `pnpm_workspace_member_names`), not an ordinary dependency manifest, but it also
    carries pnpm's own `overrides:` field -- see `_pnpm_workspace_override_names` for
    the real, live-verified gap this closes. Still needs a `PARSERS` entry so
    `find_manifests`/`parse_manifest` recognize and return it (as an ordinary scan
    target, zero dependencies when it carries no `overrides:` of its own) rather than
    raising "don't know how to parse this file" the moment a caller passes it
    directly, the same way any other recognized-but-otherwise-empty manifest is
    handled.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ManifestParseError(f"{path}: couldn't read ({e})") from e
    overrides = _pnpm_workspace_overrides(text)
    return [Dependency(name, "npm", str(path)) for name in _pnpm_workspace_override_names(overrides)]


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

    `parse_manifest` already routes any ".txt"/".in"-suffixed file to the
    requirements parser when it's given directly, since real pip/pip-tools
    doesn't care what a requirements file is named -- only a project's own
    convention does. This walk used to only ever look for the two literal
    names "requirements.txt"/"requirements.in" (`PARSERS`' own keys), so a
    real project splitting its requirements across other filenames was
    invisible to the directory-walk scan this tool's own default invocation
    (`slopcheck`, scanning the current directory) relies on, even though the
    identical file would be parsed correctly if named on the command line.
    Confirmed live against two real, currently-live examples:
    home-assistant/core's root carries "requirements.txt" (production deps)
    alongside a sibling "requirements_test.txt" (real pinned test/lint deps
    -- mypy, astroid, coverage, etc. -- neither file "-r"-including the
    other, so the second file was never reached by recursion either);
    cookiecutter-django's generated project has no top-level
    requirements.txt at all, only "requirements/base.txt",
    "requirements/local.txt", and "requirements/production.txt". Both are
    ordinary `pip install -r <file>` targets in real pip. See
    `_REQUIREMENTS_FILENAME_HINT_RE`'s own comment for why this is scoped to
    filenames/directory names mentioning "requirement" rather than every
    ".txt"/".in" file in the tree.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        dir_hint = bool(_REQUIREMENTS_FILENAME_HINT_RE.search(Path(dirpath).name))
        for filename in PARSERS:
            if filename in filenames:
                found.append(Path(dirpath) / filename)
        for filename in filenames:
            if filename in PARSERS:
                continue
            # `pylock.toml`/`pylock.<name>.toml` (see `_PYLOCK_FILENAME_RE`)
            # can't be a literal `PARSERS` key, so it needs its own scan of
            # this directory's actual filenames rather than the dict-key
            # membership check above.
            if _PYLOCK_FILENAME_RE.match(filename):
                found.append(Path(dirpath) / filename)
                continue
            if Path(filename).suffix not in (".txt", ".in"):
                continue
            if dir_hint or _REQUIREMENTS_FILENAME_HINT_RE.search(filename):
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
