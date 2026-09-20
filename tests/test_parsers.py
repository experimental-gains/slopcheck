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
        '{"dependencies": {"left-pad": "^1.0.0"}, '
        '"devDependencies": {"eslint": "^9.0.0"}}'
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"left-pad", "eslint"}
    assert all(dep.ecosystem == "npm" for dep in parse_package_json(pkg))


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
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "click", "pytest"}


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
        pytest = "^8.0"
        internal-dev-lib = { path = "../internal-dev-lib" }
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "pytest", "multi-constraint"}
