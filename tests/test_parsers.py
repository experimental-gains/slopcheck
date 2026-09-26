import json
from pathlib import Path

from slopcheck.parsers import (
    ManifestParseError,
    find_manifests,
    parse_manifest,
    parse_package_json,
    parse_pipfile,
    parse_pyproject_toml,
    parse_requirements_txt,
    parse_setup_cfg,
    requirements_txt_files_touched,
)


def test_parse_requirements_txt(tmp_path: Path):
    (tmp_path / "other.txt").write_text("pyyaml==6.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "requests==2.31.0",
                "numpy>=1.20,<2.0",
                "flask[async]==3.0.0",
                "-r other.txt",
                "-e .",
                "git+https://example.com/foo.git",
                "django",
            ]
        )
    )
    names = {dep.name for dep in parse_requirements_txt(req)}
    # "-r other.txt" is followed for real, adding pyyaml from the referenced
    # file — see test_parse_requirements_txt_recurses_into_nested_r_file
    # below for the dedicated regression coverage.
    assert names == {"requests", "numpy", "flask", "django", "pyyaml"}


def test_parse_requirements_txt_strips_inline_comments(tmp_path: Path):
    # Real-world style seen across many public requirements.txt files
    # (e.g. nuPlan's): an unpinned name followed by an explanatory comment.
    # Without stripping the comment, these dependencies were silently
    # dropped from checking entirely instead of being flagged or verified.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "\n".join(
            [
                "pandas    # Used widely",
                "pyarrow # For parquet",
                "requests==2.31.0  # pinned, has a comment too",
                "flask[async]  # extras, no version, still a comment",
            ]
        )
    )
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"pandas", "pyarrow", "requests", "flask"}


def test_parse_requirements_txt_comment_with_url_still_checks_dep(tmp_path: Path):
    # A dependency whose *explanatory comment* happens to mention a URL
    # (e.g. linking to its docs) is a normal, common style — it must not be
    # confused with an actual direct-URL install (`name @ https://...` or
    # `-e https://...`), which has no registry name to check at all and
    # should still be skipped.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "\n".join(
            [
                "requests>=2.0  # docs: https://requests.readthedocs.io",
                "flask==2.3.0  # see http://flask.palletsprojects.com",
                "-e https://github.com/foo/bar.git",
                "someurlpkg @ https://example.com/someurlpkg.whl",
            ]
        )
    )
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"requests", "flask"}


def test_parse_package_json(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"left-pad": "^1.0.0"},
                "devDependencies": {"eslint": "^9.0.0"},
                "peerDependencies": {"react": "^19.0.0"},
                "optionalDependencies": {"fsevents": "^2.3.0"},
            }
        )
    )
    deps = parse_package_json(pkg)
    names = {dep.name for dep in deps}
    assert names == {"left-pad", "eslint", "react", "fsevents"}
    assert all(dep.ecosystem == "npm" for dep in deps)
    assert all(dep.source == str(pkg) for dep in deps)


def test_parse_package_json_skips_non_registry_protocols(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "@repo/ui": "workspace:*",
                    "local-lib": "file:../local-lib",
                    "linked-lib": "link:../linked-lib",
                    "ported-lib": "portal:../ported-lib",
                    "from-git": "git+https://example.com/foo.git",
                    "gh-shorthand": "github:user/repo",
                    "react": "^19.0.0",
                },
                "devDependencies": {"@repo/eslint-config": "workspace:^"},
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"react"}


def test_parse_package_json_resolves_npm_alias_target(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "my-lodash-alias": "npm:lodash@^4.17.21",
                    "scoped-alias": "npm:@babel/core@^7.0.0",
                    "unversioned-alias": "npm:left-pad",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"lodash", "@babel/core", "left-pad", "react"}


def test_parse_package_json_reads_npm_overrides(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.19.2"},
                "overrides": {
                    "semver": "^7.5.2",
                    "foo": {
                        ".": "1.0.0",
                        "bar": "1.2.3",
                    },
                    "aliased": "npm:real-target@^1.0.0",
                },
            }
        )
    )
    deps = parse_package_json(pkg)
    names = {dep.name for dep in deps}
    assert names == {"express", "semver", "foo", "bar", "real-target"}
    assert all(dep.ecosystem == "npm" for dep in deps)
    assert all(dep.source == str(pkg) for dep in deps)


def test_parse_package_json_reads_npm_overrides_with_version_scoped_key(tmp_path: Path):
    # Real-world style seen in vscode's package.json: a key of the form
    # "pkg@version" scopes the override to only that resolved version of
    # pkg. Without stripping the "@version" suffix, the whole string got
    # checked against the registry as a package name and never matched.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"kerberos": "2.1.1"},
                "overrides": {
                    "kerberos@2.1.1": {
                        "node-addon-api": "7.1.0",
                    },
                    "@babel/core@7.20.0": "7.20.1",
                },
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"kerberos", "node-addon-api", "@babel/core"}


def test_parse_package_json_reads_pnpm_overrides(tmp_path: Path):
    # Real data from prisma/prisma's package.json: pnpm has its own
    # overrides field nested under a top-level "pnpm" key rather than
    # npm's root-level "overrides" — a manifest can have neither, either,
    # or both, so this must be read in addition to (not instead of) the
    # root-level field. Also exercises the same version-scoped-key form
    # ("minimatch@3.1.2") as npm's overrides, confirming the existing
    # suffix-stripping logic is reused correctly here too.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "devDependencies": {"typescript": "^5.0.0"},
                "pnpm": {
                    "overrides": {
                        "hono": "4.11.4",
                        "js-yaml": "3.14.2",
                        "lodash": "4.17.23",
                        "minimatch@3.1.2": "3.1.3",
                        "minimatch@9.0.5": "9.0.7",
                        "rollup": "4.59.0",
                    }
                },
            }
        )
    )
    deps = parse_package_json(pkg)
    names = {dep.name for dep in deps}
    assert names == {
        "typescript",
        "hono",
        "js-yaml",
        "lodash",
        "minimatch",
        "rollup",
    }
    assert all(dep.ecosystem == "npm" for dep in deps)
    assert all(dep.source == str(pkg) for dep in deps)


def test_parse_package_json_pnpm_key_without_overrides_is_ignored(tmp_path: Path):
    # pnpm's "pnpm" key can hold other config (patchedDependencies,
    # packageExtensions, etc.) with no "overrides" sub-key at all — must
    # not error or silently invent dependencies from unrelated pnpm config.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.19.2"},
                "pnpm": {"patchedDependencies": {"foo@1.0.0": "patches/foo.patch"}},
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"express"}


def test_parse_package_json_reads_yarn_resolutions(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.19.2"},
                "resolutions": {
                    "graceful-fs": "^4.2.11",
                    "**/lodash": "^4.17.21",
                    "webpack/**/ws": "^7.4.6",
                    "some-pkg/@babel/core": "^7.20.0",
                },
            }
        )
    )
    deps = parse_package_json(pkg)
    names = {dep.name for dep in deps}
    assert names == {"express", "graceful-fs", "lodash", "ws", "@babel/core"}
    assert all(dep.ecosystem == "npm" for dep in deps)
    assert all(dep.source == str(pkg) for dep in deps)


def test_parse_package_json_strips_range_from_yarn_resolution_key(tmp_path: Path):
    # Real patterns from jest's package.json: a resolutions key can pin a
    # range/protocol directly onto the package name with no "/" path at
    # all, or onto the name half of a scoped "@scope/name" pattern. Both
    # forms need the "@range" suffix stripped or the raw pattern (which
    # is never a real package name) gets checked against the registry
    # instead of the package it actually pins.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "resolutions": {
                    "lru-cache@^10.0.1": "patch:lru-cache@npm:10.4.3#./.yarn/patches/lru-cache.patch",
                    "@types/mdx@npm:^2.0.0": "patch:@types/mdx@npm:^2.0.0#~/.yarn/patches/types-mdx.patch",
                },
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"lru-cache", "@types/mdx"}


def test_parse_package_json_yarn_resolution_wildcard_edge_cases(tmp_path: Path):
    # A "**" wildcard segment must be dropped even when it ends up as the
    # pattern's last segment (not just when a real segment follows it) or
    # the literal "**" gets checked against the registry as if it were a
    # package name. An empty segment from a doubled "/" must also be
    # dropped, or a scoped package's "@scope" segment stops being seen as
    # the second-to-last segment and the scope silently gets dropped.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "resolutions": {
                    "webpack/**": "^5.0.0",
                    "@babel//core": "^7.20.0",
                },
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"webpack", "@babel/core"}


def test_parse_pyproject_pep621(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2", "click"]

        [project.optional-dependencies]
        dev = ["pytest"]
        """
    )
    deps = parse_pyproject_toml(pyproject)
    names = {dep.name for dep in deps}
    assert names == {"requests", "click", "pytest"}
    # Every dep found in a pyproject.toml is a PyPI dep sourced from that
    # file — both fields feed the registry-checker lookup and the reported
    # source path downstream, not just display text.
    assert all(dep.ecosystem == "pypi" for dep in deps)
    assert all(dep.source == str(pyproject) for dep in deps)


def test_parse_pyproject_pep621_parenthesized_version_specifier(tmp_path: Path):
    # PEP 508's legacy parenthesized specifier form, e.g. "numpy (>=1.16)" —
    # carried over from PEP 440/setup.py-style install_requires strings and
    # still accepted by `packaging`/pip today. Found via oracle-diff fuzzing
    # against `packaging.requirements.Requirement`: without this, the
    # dependency was silently dropped instead of checked.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["numpy (>=1.16)", "requests>=2", "click (>=8.0,<9.0); python_version>=\\"3.10\\""]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"numpy", "requests", "click"}


def test_parse_pyproject_poetry(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [tool.poetry.dependencies]
        python = "^3.10"
        requests = "^2.31"

        [tool.poetry.group.dev.dependencies]
        pytest = "^8.0"
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "pytest"}


def test_parse_pyproject_poetry_skips_non_registry_sources(tmp_path: Path):
    # Poetry's table form lets a dependency point at a git remote, a local
    # path, or a URL instead of PyPI (common for internal/private packages
    # in a monorepo) — checking these names against PyPI produces a false
    # "not found" exactly like the npm workspace:/file:/git: case above.
    # The dev group lists the non-registry dep *first* so a skip that fails
    # to keep iterating (break instead of continue) would silently drop the
    # registry dep that follows it, not just the skip itself.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [tool.poetry.dependencies]
        python = "^3.10"
        requests = "^2.31"
        internal-git-lib = { git = "https://example.com/internal-git-lib.git" }
        internal-path-lib = { path = "../internal-path-lib" }
        internal-url-lib = { url = "https://example.com/internal-url-lib.tar.gz" }
        multi-constraint = [
            { version = "^1.0", python = "<3.11" },
            { version = "^2.0", python = ">=3.11" },
        ]
        git-only-multi-constraint = [
            { git = "https://example.com/a.git", markers = "sys_platform == 'darwin'" },
            { git = "https://example.com/b.git", markers = "sys_platform == 'linux'" },
        ]

        [tool.poetry.group.dev.dependencies]
        internal-dev-lib = { path = "../internal-dev-lib" }
        pytest = "^8.0"
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "pytest", "multi-constraint"}


def test_parse_pyproject_uv_sources_skips_non_registry(tmp_path: Path):
    # uv's own source-override mechanism ([tool.uv.sources]) is the same idea
    # as Poetry's git/path/url table form above, but for a *separate* tool
    # (uv has become one of the most common Python dependency managers in
    # real current projects: litellm, crewAI, pydantic-ai, langflow, marimo,
    # and more all use it) with its own independent syntax that this parser
    # never looked at. Found via real-world testing against marimo-team/
    # marimo's actual pyproject.toml: a bare `marimo_docs` entry in
    # [project.optional-dependencies].docs, with `[tool.uv.sources]
    # marimo_docs = { path = "./docs", editable = true }` marking it as a
    # local editable install of the repo's own docs/ directory — genuinely
    # 404 on PyPI (confirmed live, both `marimo_docs` and `marimo-docs`
    # spellings), so the pre-fix parser flagged a real, legitimate dependency
    # in a ~15k-star project as a hallucinated/not-found package. `git`/
    # `path`/`workspace`/`url` sources bypass the index entirely and should
    # be skipped; `index` only redirects to a *different* index, so that name
    # still needs checking; a marker-conditional list (per-platform sources)
    # should be skipped only if every entry is non-registry.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests", "torch"]

        [project.optional-dependencies]
        docs = ["marimo_docs", "mkdocs"]

        [dependency-groups]
        dev = ["internal-git-tool", "pytest"]

        [tool.uv.sources]
        marimo_docs = { path = "./docs", editable = true }
        internal-git-tool = { git = "https://example.com/internal-git-tool.git" }
        internal-workspace-lib = { workspace = true }
        internal-url-lib = { url = "https://example.com/internal-url-lib.tar.gz" }
        torch = { index = "pytorch-cpu" }
        platform-git-lib = [
            { git = "https://example.com/a.git", marker = "sys_platform == 'darwin'" },
            { git = "https://example.com/b.git", marker = "sys_platform == 'linux'" },
        ]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "mkdocs", "pytest", "torch"}


def test_parse_pyproject_poetry_legacy_dev_dependencies(tmp_path: Path):
    # Pre-1.2 Poetry used [tool.poetry.dev-dependencies] instead of a
    # [tool.poetry.group.*.dependencies] table; both forms are still seen
    # in the wild and both need to be checked.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [tool.poetry.dependencies]
        python = "^3.10"
        requests = "^2.31"

        [tool.poetry.dev-dependencies]
        pytest = "^8.0"
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "pytest"}


def test_parse_pyproject_poetry_group_without_dependencies_key(tmp_path: Path):
    # A group table that doesn't declare a [tool.poetry.group.X.dependencies]
    # sub-table at all (e.g. a group reserved for other config) must not
    # crash the parser.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [tool.poetry.dependencies]
        python = "^3.10"
        requests = "^2.31"

        [tool.poetry.group.docs]
        optional = true
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests"}


def test_parse_pyproject_dependency_groups(tmp_path: Path):
    # PEP 735 `[dependency-groups]` — a top-level table *sibling* to
    # [project], not nested under it — found via real-world testing against
    # uv's/pytest's/pydantic's/fastapi's actual pyproject.toml files, all of
    # which use it for dev/docs/test dependencies that this parser was
    # silently never checking at all (uv's own file has zero
    # [project.dependencies], so 100% of its real deps live only here).
    # Entries can be a plain requirement spec (with extras/markers, same as
    # optional-dependencies) or an {include-group = "..."} table referencing
    # another group instead of naming a package — that reference must be
    # skipped, not mistaken for a dependency name, while the group it points
    # at still gets picked up on its own turn through the table.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2"]

        [dependency-groups]
        test = ["pytest", 'time-machine; platform_python_implementation != "PyPy"']
        docs = [
            { include-group = "test" },
            "mkdocs>=1.5.0",
        ]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "pytest", "time-machine", "mkdocs"}


def test_parse_pyproject_setuptools_dynamic_dependencies(tmp_path: Path):
    # setuptools' own dynamic-metadata mechanism
    # (https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html#dynamic-metadata):
    # `[project].dynamic` lists "dependencies"/"optional-dependencies" and defers
    # their real values to `[tool.setuptools.dynamic]`, which points at one or more
    # requirements-style files instead of listing specs inline under [project]
    # (PEP 621 requires the field be *absent* from [project] when it's dynamic).
    # Found via real-world testing against compas-dev/compas's actual
    # pyproject.toml, which uses exactly this shape — the pre-fix parser reported
    # "0 dependencies checked" against it, silently missing every real dependency.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dynamic = ["dependencies", "optional-dependencies"]

        [tool.setuptools.dynamic]
        dependencies = { file = "requirements.txt" }
        optional-dependencies.dev = { file = ["requirements-dev.txt"] }
        """
    )
    (tmp_path / "requirements.txt").write_text("jsonschema\nnetworkx >= 3.0\nnumpy >= 1.15.4\n")
    (tmp_path / "requirements-dev.txt").write_text("black >=22.12.0\npytest-cov\n")
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"jsonschema", "networkx", "numpy", "black", "pytest-cov"}


def test_parse_pyproject_setuptools_dynamic_ignored_when_not_declared_dynamic(tmp_path: Path):
    # A [tool.setuptools.dynamic] table with no corresponding entry in
    # [project].dynamic isn't actually used by setuptools for that field — must
    # not be picked up just because the table happens to be present.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests"]

        [tool.setuptools.dynamic]
        dependencies = { file = "requirements.txt" }
        """
    )
    (tmp_path / "requirements.txt").write_text("should-not-be-read\n")
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests"}


def test_find_manifests_recurses_into_workspace_packages(tmp_path: Path):
    # Regression test for a real coverage gap found via real-world testing
    # against vitejs/vite's actual repo layout: the root `package.json` of a
    # pnpm/Yarn/npm-workspaces monorepo commonly declares zero runtime
    # `dependencies` at all (vite's has none), while the published package's
    # real runtime deps live several directories down, e.g.
    # `packages/vite/package.json`. A non-recursive scan of just the root
    # directory silently missed every nested manifest — which, for a
    # supply-chain checker, means silently not checking most of the repo's
    # real dependencies. `find_manifests` must walk subdirectories.
    (tmp_path / "package.json").write_text('{"devDependencies": {"eslint": "^9.0.0"}}')
    nested = tmp_path / "packages" / "core"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text('{"dependencies": {"rolldown": "^1.0.0"}}')
    deeper = tmp_path / "apps" / "docs" / "src"
    deeper.mkdir(parents=True)
    (deeper / "pyproject.toml").write_text('[project]\nname = "docs"\ndependencies = ["mkdocs"]\n')

    found = {str(p.relative_to(tmp_path)) for p in find_manifests(tmp_path)}

    assert found == {
        "package.json",
        str(Path("packages", "core", "package.json")),
        str(Path("apps", "docs", "src", "pyproject.toml")),
    }


def test_find_manifests_prunes_node_modules_and_dot_directories(tmp_path: Path):
    # `node_modules` holds already-*installed* packages, not declared
    # dependencies — recursing into it would re-check the whole resolved
    # dependency graph (slow, and every name in it necessarily already
    # exists on the registry, so purely noise). Dot-directories (`.git`,
    # `.venv`) are pruned the same way; a `.venv` in particular can contain
    # an installed package's own `pyproject.toml`, which isn't this
    # project's declared dependency list either.
    (tmp_path / "package.json").write_text('{"dependencies": {"left-pad": "^1.0.0"}}')
    nm = tmp_path / "node_modules" / "some-installed-pkg"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name": "some-installed-pkg"}')
    venv = tmp_path / ".venv" / "lib" / "site-packages" / "pip"
    venv.mkdir(parents=True)
    (venv / "pyproject.toml").write_text('[project]\nname = "pip"\n')

    found = {str(p.relative_to(tmp_path)) for p in find_manifests(tmp_path)}

    assert found == {"package.json"}


def test_parse_requirements_txt_strips_leading_utf8_bom(tmp_path: Path):
    # Real-world find: a `requirements.txt` saved by a Windows editor/tool can
    # carry a leading UTF-8 BOM. Without stripping it, the BOM character
    # silently glued itself onto the *first* line, which then failed the
    # name-matching regex and vanished from the checked list entirely — no
    # error, no warning, just one fewer dependency checked than the file
    # actually declares.
    req = tmp_path / "requirements.txt"
    req.write_bytes(b"\xef\xbb\xbf" + b"requests==2.31.0\nflask\n")
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"requests", "flask"}


def test_parse_package_json_strips_leading_utf8_bom(tmp_path: Path):
    # Real-world find: `vitejs/vite`'s own repo ships a package.json fixture
    # with a leading UTF-8 BOM (`playground/resolve/utf8-bom-package/
    # package.json`, used to test that bundlers resolve BOM'd manifests
    # correctly) — real content real tooling handles. Before stripping the
    # BOM, `json.loads` raised on it and aborted the whole scan, not just
    # this one file.
    pkg = tmp_path / "package.json"
    pkg.write_bytes(b"\xef\xbb\xbf" + b'{"dependencies": {"left-pad": "^1.0.0"}}')
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"left-pad"}


def test_parse_pyproject_toml_strips_leading_utf8_bom(tmp_path: Path):
    # Same BOM class as the package.json/requirements.txt cases above,
    # applied to pyproject.toml: `tomllib.loads` raised "Invalid statement"
    # on a leading BOM and aborted the whole scan.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(
        b"\xef\xbb\xbf" + b'[project]\nname = "demo"\ndependencies = ["requests>=2"]\n'
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests"}


def test_parse_requirements_txt_recurses_into_nested_r_file(tmp_path: Path):
    # Real-world find: Home Assistant core's requirements_test.txt starts
    # with "-r requirements_test_pre_commit.txt"; cookiecutter-django's
    # requirements/production.txt starts with "-r base.txt". pip resolves
    # the referenced path relative to the *referencing file's own
    # directory*, and installs everything named in it for real — a
    # hallucinated name placed only in the nested file used to sail through
    # completely unchecked. Nested inside a subdirectory here specifically
    # to prove path resolution isn't CWD-relative.
    sub = tmp_path / "requirements"
    sub.mkdir()
    (sub / "base.txt").write_text("django==5.0\ntotally-fake-hallucinated-pkg==1.0\n")
    prod = sub / "production.txt"
    prod.write_text("-r base.txt\ngunicorn==22.0\n")

    names = {dep.name for dep in parse_requirements_txt(prod)}
    assert names == {"django", "totally-fake-hallucinated-pkg", "gunicorn"}


def test_requirements_txt_files_touched_includes_nested_r_file(tmp_path: Path):
    # `private_registry.pip_private_index_configured` needs the full set of
    # files pip would actually read for this scan (including anything
    # reached only via `-r`) to detect a `-i`/`--extra-index-url` directive
    # that lives in a nested file with no dependency lines of its own —
    # such a file never shows up as any `Dependency.source`, so this can't
    # be derived from `parse_requirements_txt`'s return value alone.
    sub = tmp_path / "requirements"
    sub.mkdir()
    (sub / "base.txt").write_text("--extra-index-url https://pypi.internal.example/simple\n")
    prod = sub / "production.txt"
    prod.write_text("-r base.txt\ngunicorn==22.0\n")

    touched = requirements_txt_files_touched(prod)
    assert touched == {prod.resolve(), (sub / "base.txt").resolve()}


def test_requirements_txt_files_touched_r_cycle_does_not_hang_or_crash(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("-r b.txt\npkg-a==1.0\n")
    b.write_text("-r a.txt\npkg-b==1.0\n")
    assert requirements_txt_files_touched(a) == {a.resolve(), b.resolve()}


def test_parse_requirements_txt_recurses_into_long_form_requirement_flag(tmp_path: Path):
    (tmp_path / "base.txt").write_text("requests==2.31.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text("--requirement base.txt\nflask\n")
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"requests", "flask"}


def test_parse_requirements_txt_does_not_recurse_into_constraints_file(tmp_path: Path):
    # A constraints file (-c/--constraint) only pins versions of packages
    # already required elsewhere — a name that appears *only* in it is never
    # actually installed, so it must stay unchecked (unlike -r) to avoid a
    # false positive on a name pip would never touch.
    (tmp_path / "constraints.txt").write_text("only-a-version-pin==1.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text("-c constraints.txt\nrequests\n")
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"requests"}


def test_parse_requirements_txt_missing_nested_r_file_raises_clean_error(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text("-r does-not-exist.txt\n")
    try:
        parse_requirements_txt(req)
        assert False, "expected ManifestParseError"
    except ManifestParseError as e:
        assert "does-not-exist.txt" in str(e)


def test_parse_requirements_txt_r_cycle_does_not_hang_or_crash(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("-r b.txt\npkg-a==1.0\n")
    b.write_text("-r a.txt\npkg-b==1.0\n")
    names = {dep.name for dep in parse_requirements_txt(a)}
    assert names == {"pkg-a", "pkg-b"}


def test_parse_requirements_txt_comment_on_nested_r_line(tmp_path: Path):
    (tmp_path / "base.txt").write_text("requests==2.31.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text("-r base.txt  # shared deps\nflask\n")
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"requests", "flask"}


def test_parse_manifest_routes_non_canonical_txt_filename_to_requirements_parser(tmp_path: Path):
    # Real-world find: find_manifests only auto-discovers the exact filename
    # "requirements.txt", but a real pip project often splits requirements
    # across differently-named .txt files (Home Assistant core's
    # requirements_test.txt, cookiecutter-django's requirements/local.txt).
    # A user pointing slopcheck directly at one of these used to hit
    # PARSERS[path.name] -> KeyError, an unhandled crash instead of a real
    # scan or a clean error.
    req = tmp_path / "requirements_test.txt"
    req.write_text("pytest==8.0.0\n")
    names = {dep.name for dep in parse_manifest(req)}
    assert names == {"pytest"}


def test_parse_manifest_unknown_extension_raises_clean_manifest_error(tmp_path: Path):
    weird = tmp_path / "notes.md"
    weird.write_text("not a manifest\n")
    try:
        parse_manifest(weird)
        assert False, "expected ManifestParseError"
    except ManifestParseError as e:
        assert "notes.md" in str(e)


def test_parse_pipfile(tmp_path: Path):
    # Real-world find: Pipenv's `Pipfile` (TOML, distinct from the JSON
    # `Pipfile.lock`) had no filename entry in `PARSERS`/`find_manifests` at
    # all, so a Pipenv-only project was never scanned — either a loud "no
    # manifest found" error, or, worse, a silent "0 dependencies checked,
    # all clean" whenever any other supported-but-dependency-free manifest
    # (e.g. a `pyproject.toml` used only for `[tool.ruff]` config, a common
    # real combination in Pipenv projects) happened to sit alongside it.
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        """
        [[source]]
        name = "pypi"
        url = "https://pypi.org/simple"
        verify_ssl = true

        [packages]
        requests = "*"
        flask-restplusx = "*"

        [dev-packages]
        pytest = "*"

        [requires]
        python_version = "3.11"
        """
    )
    deps = parse_pipfile(pipfile)
    names = {dep.name for dep in deps}
    assert names == {"requests", "flask-restplusx", "pytest"}
    assert all(dep.ecosystem == "pypi" for dep in deps)
    assert all(dep.source == str(pipfile) for dep in deps)


def test_parse_pipfile_skips_non_registry_sources(tmp_path: Path):
    # Pipenv's table form lets a `[packages]`/`[dev-packages]` entry point at
    # a git remote, a local path, or a local file/sdist instead of PyPI —
    # the same non-registry-source situation already handled for Poetry
    # (`_is_poetry_registry_dep`) and uv (`_is_uv_registry_source`).
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        """
        [packages]
        requests = "*"
        internal-git-lib = { git = "https://example.com/internal-git-lib.git" }
        internal-path-lib = { path = "./vendor/internal-path-lib" }
        internal-file-lib = { file = "https://example.com/internal-file-lib.tar.gz" }

        [dev-packages]
        pytest = "*"
        internal-dev-lib = { path = "../internal-dev-lib" }
        """
    )
    names = {dep.name for dep in parse_pipfile(pipfile)}
    assert names == {"requests", "pytest"}


def test_find_manifests_discovers_pipfile(tmp_path: Path):
    (tmp_path / "Pipfile").write_text('[packages]\nrequests = "*"\n')
    found = {p.name for p in find_manifests(tmp_path)}
    assert "Pipfile" in found


def test_parse_manifest_routes_pipfile_to_pipfile_parser(tmp_path: Path):
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text('[packages]\nrequests = "*"\n')
    names = {dep.name for dep in parse_manifest(pipfile)}
    assert names == {"requests"}


def test_parse_setup_cfg(tmp_path: Path):
    # Real-world find: setuptools' legacy `setup.cfg` `[options]
    # install_requires` had no filename entry in `PARSERS`/`find_manifests`
    # at all — confirmed live against RDFLib/sparqlwrapper and
    # rm-hull/luma.oled, two real, current packages that declare their
    # actual dependencies this way while still shipping a `pyproject.toml`
    # containing only `[build-system]` (required by PEP 518 for pip to
    # build them at all, but not itself a dependency declaration). Before
    # this fix, that `pyproject.toml` was the only manifest `find_manifests`
    # recognized, and it legitimately parses to zero dependencies — a
    # silent "0 dependencies checked, all clean" false-all-clear, the same
    # failure shape as the `Pipfile` gap fixed above.
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[metadata]\n"
        "name = example\n"
        "\n"
        "[options]\n"
        "install_requires =\n"
        "    requests>=2.31.0  # http client\n"
        "    flask[async]==3.0.0\n"
        "    ; a full-line comment\n"
        "    numpy>=1.20,<2.0\n"
        "\n"
        "[options.extras_require]\n"
        "test =\n"
        "    pytest\n"
        "    pytest-cov\n"
        "docs =\n"
        "    sphinx\n"
    )
    deps = parse_setup_cfg(cfg)
    names = {dep.name for dep in deps}
    assert names == {"requests", "flask", "numpy", "pytest", "pytest-cov", "sphinx"}
    assert all(dep.ecosystem == "pypi" for dep in deps)
    assert all(dep.source == str(cfg) for dep in deps)


def test_parse_setup_cfg_skips_urls_and_missing_sections(tmp_path: Path):
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[options]\n"
        "install_requires =\n"
        "    requests\n"
        "    internal-lib @ https://example.com/internal-lib.tar.gz\n"
    )
    names = {dep.name for dep in parse_setup_cfg(cfg)}
    assert names == {"requests"}


def test_find_manifests_discovers_setup_cfg(tmp_path: Path):
    (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires =\n    requests\n")
    found = {p.name for p in find_manifests(tmp_path)}
    assert "setup.cfg" in found


def test_parse_manifest_routes_setup_cfg_to_setup_cfg_parser(tmp_path: Path):
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[options]\ninstall_requires =\n    requests\n")
    names = {dep.name for dep in parse_manifest(cfg)}
    assert names == {"requests"}
