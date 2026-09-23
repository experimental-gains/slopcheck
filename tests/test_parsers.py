import json
from pathlib import Path

from slopcheck.parsers import (
    parse_package_json,
    parse_pyproject_toml,
    parse_requirements_txt,
)


def test_parse_requirements_txt(tmp_path: Path):
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
    assert names == {"requests", "numpy", "flask", "django"}


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
