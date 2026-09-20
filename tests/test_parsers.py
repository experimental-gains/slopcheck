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
