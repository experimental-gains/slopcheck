import json
from pathlib import Path

from slopcheck.parsers import (
    ManifestParseError,
    environment_yml_files_touched,
    find_manifests,
    npm_workspace_member_names,
    npm_workspace_root,
    parse_environment_yml,
    parse_manifest,
    parse_package_json,
    parse_pipfile,
    parse_pixi_toml,
    parse_pre_commit_config,
    parse_pylock_toml,
    parse_pyproject_toml,
    parse_requirements_txt,
    parse_setup_cfg,
    parse_tox_ini,
    parse_tox_toml,
    pdm_workspace_member_names,
    pdm_workspace_root,
    pnpm_workspace_member_names,
    pnpm_workspace_root,
    requirements_txt_files_touched,
    uv_workspace_root,
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


def test_parse_requirements_txt_joins_backslash_continuation(tmp_path: Path):
    # pip's own requirements-file preprocessor (req_file.py's `join_lines`)
    # joins a line ending in an unescaped `\` with the line(s) that follow it
    # *before* it ever tries to parse a requirement out of it — real, current
    # syntax `pip-compile --generate-hashes` emits routinely for a spec too
    # long to fit on one line, e.g. an extras list wrapping the version onto
    # its own continuation line. Confirmed live (real pip 25.1.1, `pip
    # install --dry-run -r`, this exact two-line shape): pip genuinely joins
    # them into one logical requirement, "totally-hallucinated-xyz-987
    # ==1.2.3", and fails resolving it exactly like any other hallucinated
    # name — "ERROR: Could not find a version that satisfies the requirement
    # totally-hallucinated-xyz-987==1.2.3 (from versions: none)".
    #
    # Before this fix, `_parse_requirements_txt` walked `text.splitlines()`
    # with no continuation-joining at all: the first physical line
    # ("totally-hallucinated-xyz-987 \") has a trailing backslash `_REQ_LINE_RE`
    # can't absorb (no alternation branch matches a lone "\"), so the whole
    # line fails to match and is silently dropped; the second physical line
    # ("    ==1.2.3") doesn't start with a name character either, so it's
    # dropped too. The name never became a `Dependency` at all — a real
    # `pip install -r` would genuinely try to fetch it, but slopcheck reported
    # a clean scan.
    req = tmp_path / "requirements.txt"
    req.write_text(
        "\n".join(
            [
                "totally-hallucinated-xyz-987 \\",
                "    ==1.2.3",
                "real-onefile-dep==1.0.0 \\",
                "    --hash=sha256:aaaa \\",
                "    --hash=sha256:bbbb",
            ]
        )
    )
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"totally-hallucinated-xyz-987", "real-onefile-dep"}


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


def test_npm_workspace_member_names_from_array_and_object_forms(tmp_path: Path):
    # Regression test for a real-world find: confirmed live (npm 9.2.0 and
    # Yarn Classic 1.22.22) that a plain `"workspaces": ["packages/*"]`
    # array declares real, local-only sibling packages a real
    # `npm install`/`yarn install` resolves entirely locally (symlinked, no
    # registry request) whenever a dependent names one with an ordinary
    # semver range -- not just the explicit `workspace:` protocol form
    # `_NON_REGISTRY_PREFIXES` already skips. Yarn Classic's own
    # object-form `"workspaces": {"packages": [...], "nohoist": [...]}` is
    # equally real and must resolve the same set of names, ignoring the
    # unrelated `nohoist` hint.
    root = tmp_path / "array-form"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}))
    member = root / "packages" / "internal-lib"
    member.mkdir(parents=True)
    (member / "package.json").write_text(json.dumps({"name": "@scratch/internal-lib", "private": True}))

    obj_root = tmp_path / "object-form"
    obj_root.mkdir()
    (obj_root / "package.json").write_text(
        json.dumps({"workspaces": {"packages": ["packages/*"], "nohoist": ["**/react"]}})
    )
    obj_member = obj_root / "packages" / "other-lib"
    obj_member.mkdir(parents=True)
    (obj_member / "package.json").write_text(json.dumps({"name": "@scratch/other-lib"}))

    pairs = npm_workspace_member_names(
        [root / "package.json", member / "package.json", obj_root / "package.json", obj_member / "package.json"]
    )

    pairs_by_root = dict(pairs)
    assert pairs_by_root[root] == {"@scratch/internal-lib"}
    assert pairs_by_root[obj_root] == {"@scratch/other-lib"}


def test_npm_workspace_member_names_ignores_non_workspace_package_json(tmp_path: Path):
    plain = tmp_path / "package.json"
    plain.write_text(json.dumps({"dependencies": {"left-pad": "^1.0.0"}}))

    assert npm_workspace_member_names([plain]) == []


def test_npm_workspace_root_finds_ancestor_with_matching_workspaces_pattern(tmp_path: Path):
    # Regression test for a real-world find: confirmed live (npm 9.2.0) that
    # `.npmrc` resolution for a workspace *member* directory walks up to the
    # enclosing workspace root (debug log: "found workspace root at ...")
    # rather than stopping at the member's own package.json -- see
    # `npm_workspace_root`'s docstring for the full live repro. This matters
    # for slopcheck because the real scan is sometimes rooted at just the
    # member directory, and the workspace root's package.json never shows up
    # among the scanned manifests at all in that case.
    root = tmp_path / "root"
    member = root / "packages" / "foo"
    member.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "root", "workspaces": ["packages/*"]}))
    (member / "package.json").write_text(json.dumps({"name": "foo"}))

    assert npm_workspace_root(member) == root


def test_npm_workspace_root_none_without_workspaces_field(tmp_path: Path):
    # The mirror image: confirmed live that a plain nested package.json with
    # no enclosing `workspaces` field does *not* get this treatment -- real
    # npm treats the nested package.json as its own project root. Matching
    # on a mere ancestor package.json's *presence*, without checking its
    # `workspaces` patterns actually resolve to the start directory, would
    # risk wrongly suppressing a genuine hallucination elsewhere.
    root = tmp_path / "root"
    member = root / "sub"
    member.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "root"}))
    (member / "package.json").write_text(json.dumps({"name": "sub"}))

    assert npm_workspace_root(member) is None


def test_npm_workspace_root_none_with_no_ancestor_package_json(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert npm_workspace_root(nested) is None


def test_uv_workspace_root_finds_ancestor_with_matching_members_pattern(tmp_path: Path):
    # Regression test for a real-world find: confirmed live (uv 0.12.19)
    # that `uv lock` run from inside a workspace *member* directory walks up
    # and discovers the enclosing workspace root pyproject.toml (debug log:
    # "Found static `pyproject.toml` for: ws-root @ ...") and consults that
    # root's own private-index config for the member's dependency -- see
    # `uv_workspace_root`'s docstring for the full live repro. This matters
    # for slopcheck because a real scan is sometimes rooted at just the
    # member directory (a monorepo CI job scoped to one changed package),
    # and the workspace root's pyproject.toml never shows up among the
    # scanned manifests at all in that case.
    root = tmp_path / "root"
    member = root / "pkgs" / "foo"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\n\n[tool.uv.workspace]\nmembers = ["pkgs/*"]\n'
    )
    (member / "pyproject.toml").write_text('[project]\nname = "foo"\n')

    assert uv_workspace_root(member) == root


def test_uv_workspace_root_respects_exclude(tmp_path: Path):
    # Confirmed live (uv 0.12.19): a directory matched by `members` but also
    # matched by `exclude` is genuinely treated as *outside* the workspace
    # -- its own `uv lock` only ever contacted public PyPI, never the
    # workspace root's configured private index, even though its path
    # matches the `members` glob too.
    root = tmp_path / "root"
    member = root / "pkgs" / "excluded"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\n\n'
        "[tool.uv.workspace]\n"
        'members = ["pkgs/*"]\n'
        'exclude = ["pkgs/excluded"]\n'
    )
    (member / "pyproject.toml").write_text('[project]\nname = "excluded"\n')

    assert uv_workspace_root(member) is None


def test_uv_workspace_root_none_without_workspace_table(tmp_path: Path):
    # The mirror image: a plain nested pyproject.toml with no enclosing
    # `[tool.uv.workspace]` does not get this treatment -- real uv treats
    # the nested pyproject.toml as its own standalone project. Matching on a
    # mere ancestor pyproject.toml's presence, without checking its
    # `members` patterns actually resolve to the start directory, would risk
    # wrongly suppressing a genuine hallucination elsewhere.
    root = tmp_path / "root"
    member = root / "sub"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "root"\n')
    (member / "pyproject.toml").write_text('[project]\nname = "sub"\n')

    assert uv_workspace_root(member) is None


def test_uv_workspace_root_none_with_no_ancestor_pyproject(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert uv_workspace_root(nested) is None


def test_pnpm_workspace_member_names_from_separate_yaml_file(tmp_path: Path):
    # Regression test for a real-world find: pnpm declares workspace
    # membership an entirely different way from npm/Yarn Classic
    # (`test_npm_workspace_member_names_from_array_and_object_forms` above)
    # -- a sibling `pnpm-workspace.yaml`, not `package.json`'s `workspaces`
    # field at all (pnpm never reads that field). Confirmed live (pnpm
    # 9.15.0, a from-scratch two-package pnpm workspace with
    # `link-workspace-packages=true` set in a root `.npmrc` -- a real,
    # documented pnpm setting, the default before pnpm 8 and still commonly
    # carried over in older/migrated monorepos): a root `devDependencies`
    # entry naming a sibling workspace member with an ordinary semver range
    # (no `workspace:` protocol) resolved entirely locally -- `pnpm install`
    # symlinked the local directory with zero requests to
    # `registry.npmjs.org` for that name. Before this fix, `pnpm_workspace_
    # member_names` didn't exist at all and `pnpm-workspace.yaml` was never
    # read, so a pnpm monorepo shaped this way had every private, unpublished
    # workspace member reported as a plain `not_found` hallucination.
    root = tmp_path / "pnpm-monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
    member = root / "packages" / "internal-lib"
    member.mkdir(parents=True)
    (member / "package.json").write_text(json.dumps({"name": "@scratch/pnpm-internal-lib", "private": True}))

    pairs = pnpm_workspace_member_names([root / "pnpm-workspace.yaml"])

    assert dict(pairs) == {root: {"@scratch/pnpm-internal-lib"}}


def test_pnpm_workspace_member_names_ignores_missing_or_empty_packages_key(tmp_path: Path):
    no_packages = tmp_path / "no-packages" / "pnpm-workspace.yaml"
    no_packages.parent.mkdir()
    no_packages.write_text("onlyBuiltDependencies:\n  - some-native-pkg\n")

    assert pnpm_workspace_member_names([no_packages]) == []


def test_pnpm_workspace_root_finds_ancestor_with_matching_packages_pattern(tmp_path: Path):
    # Regression test for a real-world find: pnpm declares workspace
    # membership in a sibling `pnpm-workspace.yaml` that lives at the
    # workspace *root*, never inside a member's own directory -- so a
    # member scanned on its own has no way to see it unless something
    # walks up looking for it, the same gap already closed for npm
    # (`npm_workspace_root`), uv (`uv_workspace_root`), and PDM
    # (`pdm_workspace_root`), just never ported to pnpm's own,
    # differently-shaped workspace file. Confirmed live (pnpm 12.8.1): see
    # `pnpm_workspace_root`'s own docstring for the full repro -- pnpm
    # genuinely discovers the workspace root by walking up from the current
    # directory, and a plain-semver-range dependency on a sibling member
    # resolved entirely locally (symlinked, zero registry requests) when
    # `pnpm install` ran from inside the member directory itself.
    root = tmp_path / "pnpm-monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
    member = root / "packages" / "app"
    member.mkdir(parents=True)

    assert pnpm_workspace_root(member) == root


def test_pnpm_workspace_root_none_without_packages_key(tmp_path: Path):
    # The mirror image: an ancestor `pnpm-workspace.yaml` with no `packages`
    # list at all (e.g. one that only sets `onlyBuiltDependencies`) does not
    # claim any directory as a member.
    root = tmp_path / "root"
    member = root / "packages" / "app"
    member.mkdir(parents=True)
    (root / "pnpm-workspace.yaml").write_text("onlyBuiltDependencies:\n  - some-native-pkg\n")

    assert pnpm_workspace_root(member) is None


def test_pnpm_workspace_root_none_when_packages_pattern_does_not_match(tmp_path: Path):
    # An ancestor `pnpm-workspace.yaml` present but whose `packages` glob
    # doesn't actually resolve to `start` must not match -- mirrors
    # `uv_workspace_root`'s/`pdm_workspace_root`'s identical "ancestor
    # present but pattern doesn't cover this directory" guard.
    root = tmp_path / "root"
    member = root / "not-members" / "app"
    member.mkdir(parents=True)
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")

    assert pnpm_workspace_root(member) is None


def test_pnpm_workspace_root_finds_ancestor_with_commented_packages_pattern(tmp_path: Path):
    # Regression test for a real-world find: `_pnpm_workspace_patterns` had
    # zero handling for a trailing `#` comment anywhere in
    # `pnpm-workspace.yaml`, a real, common annotation on either the glob
    # itself or the `packages:` header line (valid YAML everywhere a value
    # isn't being spelled out). Confirmed live (no mocking, direct function
    # test): a comment on the glob line folded the comment text into the
    # pattern itself ("packages/*'  # keep in sync..."), which then never
    # matched any real directory via `root.glob()`, so a legitimate local
    # workspace member was reported as an unresolved `not_found`
    # hallucination instead of recognized and skipped.
    root = tmp_path / "pnpm-monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'  # keep in sync with CI matrix\n")
    member = root / "packages" / "app"
    member.mkdir(parents=True)

    assert pnpm_workspace_root(member) == root


def test_pnpm_workspace_root_finds_ancestor_with_commented_packages_header(tmp_path: Path):
    # The other manifestation of the same gap: a comment on the `packages:`
    # header line itself (e.g. `packages:  # list of workspace globs`) made
    # `top_match.group(2)` read as a non-empty inline value, so the whole
    # block was wrongly treated as not-a-block-sequence and every pattern
    # inside it -- not just one -- was silently dropped.
    root = tmp_path / "pnpm-monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:  # list of workspace globs\n  - 'packages/*'\n")
    member = root / "packages" / "app"
    member.mkdir(parents=True)

    assert pnpm_workspace_root(member) == root


def test_pnpm_workspace_root_none_with_no_ancestor_workspace_file(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert pnpm_workspace_root(nested) is None


def test_pnpm_workspace_member_names_discovers_root_from_member_alone(tmp_path: Path):
    # Regression test for the real-world find above, through
    # `pnpm_workspace_member_names` itself: scanning *only* the member's own
    # `package.json` (the root's `pnpm-workspace.yaml` is never passed in
    # `pnpm_workspace_paths` at all -- it isn't even among the scanned
    # manifests, the exact realistic shape a monorepo CI job or pre-commit
    # hook scoped to one changed package produces) must still recognize a
    # sibling member's declared name as locally-resolved. Before this fix,
    # `pnpm_workspace_member_names` had no `package_json_paths` parameter at
    # all and could never discover a root that wasn't passed directly, so
    # `slopcheck` scanning just `packages/app` reported the sibling's name a
    # plain `not_found` hallucination -- confirmed live against real pnpm
    # 12.8.1 (see `pnpm_workspace_root`'s own docstring for the full repro).
    root = tmp_path / "pnpm-monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
    member = root / "packages" / "app"
    member.mkdir(parents=True)
    (member / "package.json").write_text(json.dumps({"name": "app", "dependencies": {"internal-lib": "^1.0.0"}}))
    sibling = root / "packages" / "internal-lib"
    sibling.mkdir(parents=True)
    (sibling / "package.json").write_text(json.dumps({"name": "internal-lib", "private": True}))

    pairs = pnpm_workspace_member_names([], [member / "package.json"])

    assert dict(pairs) == {root: {"app", "internal-lib"}}


def test_pnpm_workspace_member_names_without_package_json_paths_still_works(tmp_path: Path):
    # The `package_json_paths` parameter is optional -- scanning the whole
    # tree (where the root's `pnpm-workspace.yaml` is already among
    # `pnpm_workspace_paths`) must behave exactly as before this fix, with
    # no `package_json_paths` argument at all.
    root = tmp_path / "pnpm-monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
    member = root / "packages" / "internal-lib"
    member.mkdir(parents=True)
    (member / "package.json").write_text(json.dumps({"name": "@scratch/pnpm-internal-lib", "private": True}))

    pairs = pnpm_workspace_member_names([root / "pnpm-workspace.yaml"])

    assert dict(pairs) == {root: {"@scratch/pnpm-internal-lib"}}


def test_parse_manifest_reads_pnpm_workspace_yaml_overrides(tmp_path: Path):
    # Regression test for a real-world find: pnpm's own `overrides` field
    # (https://pnpm.io/settings/dependency-resolution#overrides) can live
    # directly in `pnpm-workspace.yaml` -- a real, current, documented
    # location for it, distinct from package.json's `pnpm.overrides`
    # (already read by `parse_package_json`). Confirmed live (pnpm 12.8.1):
    # `overrides: {is-number: 'npm:totally-hallucinated-pnpm-ws-override-
    # alias-xyz-321@1.0.0'}`, with `is-number` a real transitive dependency
    # of `is-odd` named in no package.json at all, made `pnpm install`
    # genuinely issue `GET https://registry.npmjs.org/totally-hallucinated-
    # pnpm-ws-override-alias-xyz-321` and fail with a real 404 -- `is-number`
    # itself was never fetched under its own name once overridden. Before
    # this fix, `_parse_pnpm_workspace_yaml` discarded the file's entire
    # body unconditionally (it only existed so `parse_manifest` recognized
    # the filename at all), so this real, install-breaking hallucinated
    # override target was invisible to slopcheck no matter what.
    #
    # Also covers pnpm's own `"parent@version>dependency"` override-key
    # scoping syntax (only the segment after the last `>` is the real
    # dependency being overridden, confirmed against pnpm's own docs) and
    # the literal `"-"` value (pnpm's documented "remove this dependency"
    # syntax -- never fetched by real pnpm, so not a checkable name).
    path = tmp_path / "pnpm-workspace.yaml"
    path.write_text(
        "packages:\n"
        "  - 'packages/*'\n"
        "overrides:\n"
        "  is-number: 'npm:totally-hallucinated-pnpm-ws-override-alias-xyz-321@1.0.0'\n"
        "  \"qar@1>zoo\": '2'\n"
        "  lodash: '-'\n"
        "  foo: '^1.0.0'\n"
    )

    names = {dep.name for dep in parse_manifest(path)}

    assert names == {"totally-hallucinated-pnpm-ws-override-alias-xyz-321", "zoo", "foo"}
    assert "is-number" not in names
    assert "lodash" not in names


def test_parse_manifest_pnpm_workspace_yaml_overrides_skips_non_registry_value(tmp_path: Path):
    # Confirmed live (pnpm 12.8.1): overrides: {wrappy: 'http://127.0.0.1
    # :8911/wrappy-1.0.2.tgz'} resolved "wrappy" purely by URL -- an HTTP
    # server log showed zero requests to registry.npmjs.org for it at
    # all. Before this fix, the override key was always checked against
    # the registry, so a legitimate tarball-URL/file:/git: override got
    # reported as a hallucinated package.
    path = tmp_path / "pnpm-workspace.yaml"
    path.write_text(
        "packages:\n"
        "  - 'packages/*'\n"
        "overrides:\n"
        "  wrappy: 'http://127.0.0.1:8911/wrappy-1.0.2.tgz'\n"
        "  foo: '^1.0.0'\n"
    )

    names = {dep.name for dep in parse_manifest(path)}

    assert names == {"foo"}
    assert "wrappy" not in names


def test_parse_manifest_pnpm_workspace_yaml_with_no_overrides_key_is_empty(tmp_path: Path):
    path = tmp_path / "pnpm-workspace.yaml"
    path.write_text("packages:\n  - 'packages/*'\n")

    assert parse_manifest(path) == []


def test_parse_manifest_pnpm_workspace_yaml_overrides_header_comment(tmp_path: Path):
    # Regression test for a real-world find: `_pnpm_workspace_overrides` had
    # zero handling for a trailing `#` comment anywhere in the `overrides:`
    # block -- the exact same gap `_pnpm_workspace_patterns` (this same
    # file's `packages:` reader) and `_environment_yml_pip_requirement_lines`
    # were both already fixed for, just never ported to this later-added
    # sibling function. Confirmed live (pnpm 12.8.1, real `pnpm install`): a
    # root `pnpm-workspace.yaml` with `overrides:  # pin overrides for CVEs`
    # followed by `is-number: npm:totally-hallucinated-pwyo-headercomment-
    # xyz-789@1.0.0`, and a member depending on the real `is-number` with no
    # other override, genuinely redirected `is-number`'s fetch to the alias
    # target and failed with a real `GET https://registry.npmjs.org/
    # totally-hallucinated-pwyo-headercomment-xyz-789: Not Found - 404` --
    # pnpm's own YAML parser strips the header comment and still applies the
    # override. Before this fix, the comment made `top_match.group(2)` read
    # as a non-empty inline value, so `in_overrides` was wrongly left
    # `False` and the entire `overrides:` block -- hallucinated alias target
    # included -- was silently dropped, reproduced directly: `parse_manifest`
    # returned zero dependencies for this exact file instead of flagging the
    # alias target.
    path = tmp_path / "pnpm-workspace.yaml"
    path.write_text(
        "packages:\n"
        "  - 'packages/*'\n"
        "overrides:  # pin overrides for CVEs\n"
        "  is-number: npm:totally-hallucinated-pwyo-headercomment-xyz-789@1.0.0\n"
    )

    names = {dep.name for dep in parse_manifest(path)}

    assert names == {"totally-hallucinated-pwyo-headercomment-xyz-789"}


def test_parse_manifest_pnpm_workspace_yaml_overrides_entry_comment(tmp_path: Path):
    # The other manifestation of the same gap: a trailing comment on an
    # *entry* line corrupts the extracted value instead of hiding the whole
    # block. Confirmed live (pnpm 12.8.1): `is-number: npm:is-odd  # alias
    # without pinned version, see security advisory` resolves `is-number`
    # to the real, existing `is-odd` package (pnpm's own lockfile records
    # `specifier: npm:is-odd, version: is-odd@3.0.1`, zero registry errors)
    # -- real pnpm's YAML parser strips the comment before ever looking at
    # the value. Before this fix, `_npm_alias_target` found no `@` in the
    # unstripped `"is-odd  # alias without pinned version, see security
    # advisory"` (there's no pinned version to stop at) and returned the
    # whole polluted string as the "name," which got checked against the
    # registry and reported `not_found` -- a false positive on a dependency
    # a real `pnpm install` resolves cleanly.
    path = tmp_path / "pnpm-workspace.yaml"
    path.write_text(
        "packages:\n"
        "  - 'packages/*'\n"
        "overrides:\n"
        "  is-number: npm:is-odd  # alias without pinned version, see security advisory\n"
    )

    names = {dep.name for dep in parse_manifest(path)}

    assert names == {"is-odd"}


def test_pdm_workspace_member_names_from_tool_pdm_workspace(tmp_path: Path):
    # Regression test for a real-world find: PDM's own workspace feature
    # (https://pdm-project.org/latest/usage/workspace/, added 2.28.0) is a
    # *third* monorepo-membership mechanism, distinct from both uv's
    # `[tool.uv.workspace]` (which additionally requires a matching
    # `[tool.uv.sources] name = { workspace = true }` entry before a plain
    # dependency on a member resolves locally -- confirmed live, real `uv
    # lock` Fatals without it: "is included as a workspace member, but is
    # missing an entry in tool.uv.sources") and npm/pnpm's package.json-/
    # pnpm-workspace.yaml-based mechanisms. Confirmed live (PDM 2.29.2, `pdm
    # lock -v` against a from-scratch two-project workspace: root
    # pyproject.toml with `dependencies =
    # ["totally-hallucinated-pdm-workspace-xyz-123"]` and `[tool.pdm.workspace]
    # members = ["packages/*"]`, member `packages/bar/pyproject.toml` naming
    # itself `totally-hallucinated-pdm-workspace-xyz-123`, no
    # `[tool.pdm.sources]` anywhere): `pdm lock` resolved the dependency
    # entirely locally ("The file packages/bar is a local directory, use it
    # directly" / "Adding new pin: totally-hallucinated-pdm-workspace-xyz-123
    # file:///${PROJECT_ROOT}/packages/bar") with zero PyPI requests. Before
    # this fix, `pdm_workspace_member_names` didn't exist at all and
    # `[tool.pdm.workspace]` was never read, so a PDM workspace shaped this
    # way had every private, unpublished workspace member reported as a plain
    # `not_found` hallucination.
    root = tmp_path / "pdm-monorepo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "foo"\n\n[tool.pdm.workspace]\nmembers = ["packages/*"]\n'
    )
    member = root / "packages" / "bar"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname = "internal-bar"\n')

    pairs = pdm_workspace_member_names([root / "pyproject.toml"])

    assert dict(pairs) == {root: {"internal-bar"}}


def test_pdm_workspace_member_names_ignores_missing_or_empty_members(tmp_path: Path):
    no_members = tmp_path / "no-members" / "pyproject.toml"
    no_members.parent.mkdir()
    no_members.write_text('[project]\nname = "foo"\n\n[tool.pdm]\n')

    assert pdm_workspace_member_names([no_members]) == []


def test_pdm_workspace_root_finds_ancestor_with_matching_members_pattern(tmp_path: Path):
    # Regression test for a real-world find: PDM's own `pdm lock`/`pdm
    # install` both hard-error ("can only be run from the workspace root")
    # when invoked from inside a member directory, so the *only* way a real
    # resolution ever happens is from the root -- confirmed live (PDM
    # 2.29.2) that running from the root genuinely consults the root's own
    # `[tool.pdm.workspace].members` list for a member's sibling-dependency
    # resolution and the root's own `[[tool.pdm.source]]`/config files for a
    # member's own plain dependencies. Scanning just the member directory
    # (a realistic monorepo-CI shape, the exact pattern already fixed for
    # uv via `uv_workspace_root`) never saw the root's pyproject.toml at
    # all before this fix -- see `pdm_workspace_root`'s own docstring for
    # the full live repro.
    root = tmp_path / "root"
    member = root / "packages" / "foo"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\n\n[tool.pdm.workspace]\nmembers = ["packages/*"]\n'
    )
    (member / "pyproject.toml").write_text('[project]\nname = "foo"\n')

    assert pdm_workspace_root(member) == root


def test_pdm_workspace_root_none_without_workspace_table(tmp_path: Path):
    # The mirror image: a plain nested pyproject.toml with no enclosing
    # `[tool.pdm.workspace]` does not get this treatment.
    root = tmp_path / "root"
    member = root / "sub"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "root"\n')
    (member / "pyproject.toml").write_text('[project]\nname = "sub"\n')

    assert pdm_workspace_root(member) is None


def test_pdm_workspace_root_none_when_members_pattern_does_not_match(tmp_path: Path):
    # An ancestor pyproject.toml declaring `[tool.pdm.workspace]` whose
    # `members` glob doesn't actually resolve to `start` must not match --
    # mirrors `uv_workspace_root`'s identical "ancestor present but pattern
    # doesn't cover this directory" guard.
    root = tmp_path / "root"
    member = root / "notmembers" / "foo"
    member.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\n\n[tool.pdm.workspace]\nmembers = ["packages/*"]\n'
    )
    (member / "pyproject.toml").write_text('[project]\nname = "foo"\n')

    assert pdm_workspace_root(member) is None


def test_pdm_workspace_root_none_with_no_ancestor_pyproject(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert pdm_workspace_root(nested) is None


def test_pdm_workspace_member_names_discovers_root_from_member_alone(tmp_path: Path):
    # Regression test for the real-world find above, through
    # `pdm_workspace_member_names` itself: scanning *only* the member's own
    # pyproject.toml (the root's is never passed in at all) must still
    # recognize a sibling member's declared name as locally-resolved --
    # confirmed live (PDM 2.29.2) that `pdm lock`, run from the root (the
    # only way PDM allows it), resolves a member's dependency on a sibling
    # member entirely from the local checkout, zero PyPI involvement.
    # Before this fix, slopcheck scanning just `packages/member` reported
    # the sibling's name a plain `not_found` hallucination.
    root = tmp_path / "pdm-monorepo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ws-root"\n\n[tool.pdm.workspace]\nmembers = ["packages/*"]\n'
    )
    member = root / "packages" / "member"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname = "member"\ndependencies = ["internal-bar"]\n'
    )
    sibling = root / "packages" / "bar"
    sibling.mkdir(parents=True)
    (sibling / "pyproject.toml").write_text('[project]\nname = "internal-bar"\n')

    pairs = pdm_workspace_member_names([member / "pyproject.toml"])

    assert dict(pairs) == {root: {"member", "internal-bar"}}


def test_parse_package_json_skips_bare_github_shorthand_and_other_git_hosts(tmp_path: Path):
    # Confirmed live (npm 9.2.0, `npm install --dry-run --loglevel=verbose`):
    # a bare "user/repo" version value (no "github:" prefix at all — npm
    # defaults un-prefixed host shorthand to GitHub) makes npm run
    # `git ls-remote ssh://git@github.com/sindresorhus/is-odd.git` and never
    # contact the npm registry for the dependency's name at all, regardless
    # of whether the named repo is real. "gitlab:user/repo" and
    # "bitbucket:user/repo" dispatch the same way to their own hosts. None
    # of these three forms were in `_NON_REGISTRY_PREFIXES` (only the
    # "github:"-prefixed spelling was), so each hallucinated key here used
    # to be checked against the public npm registry and flagged not_found —
    # a false positive on a legitimate, real npm dependency shape.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "bare-shorthand": "sindresorhus/is-odd",
                    "gitlab-shorthand": "gitlab:user/repo",
                    "bitbucket-shorthand": "bitbucket:user/repo",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"react"}


def test_parse_package_json_skips_gist_shorthand(tmp_path: Path):
    # npm's own docs list "gist:" as a fourth documented git-host shorthand
    # alongside "github:"/"gitlab:"/"bitbucket:" ("you may also specify the
    # gist:, bitbucket:, gitlab:, and github: prefixes explicitly"), and
    # npm-package-arg's HostedGit.fromUrl() dispatches it to git exactly like
    # the other three. Unlike them, a real gist reference is just a bare gist
    # ID with no "/" at all, so the "/"-based fallback that catches the other
    # hosts' bare/prefixed shorthand never fires for it. Confirmed live (npm
    # 11.20.0, `npm install`): a hallucinated dependency name paired with a
    # "gist:"-prefixed version value made npm run `git --no-replace-objects
    # ls-remote ssh://git@gist.github.com/<id>.git` and never contact the
    # npm registry for that name at all, regardless of whether the gist
    # exists -- the same false-positive shape already fixed for the other
    # three git-host shorthands, just missed here because "gist:" alone was
    # never in `_NON_REGISTRY_PREFIXES` and has no slash to trip the fallback.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "gist-dep": "gist:101a11beef",
                    "gist-dep-with-slash": "gist:someuser/101a11beef",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"react"}


def test_parse_package_json_skips_exec_protocol(tmp_path: Path):
    # Yarn Berry's own "exec:" protocol (https://yarnpkg.com/features/protocols#exec)
    # builds a package on the fly from a local script instead of fetching it
    # from any registry. A real descriptor is just a relative path to that
    # script and, unlike the git-host shorthand forms above, needs no "/" at
    # all when the script sits right next to package.json -- so the
    # slash-based fallback that catches github:/gitlab:/bitbucket: shorthand
    # never fires for it. Confirmed live (Yarn Berry 4.18.1, `yarn install`,
    # scripts enabled): a scratch package.json with a sibling `builder.js`
    # and `"totally-hallucinated-execprotocol-xyz-556": "exec:builder.js"`
    # failed resolution with "...@exec:builder.js...: Manifest not found" --
    # a purely local-filesystem error raised before Yarn's install even
    # reaches its Fetch step -- and made zero requests to
    # registry.yarnpkg.com/registry.npmjs.org for that name.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "exec-dep": "exec:builder.js",
                    "exec-dep-with-slash": "exec:./scripts/builder.js",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"react"}


def test_parse_package_json_skips_bare_dot_local_path(tmp_path: Path):
    # npm-package-arg's own resolve() (read directly from
    # /usr/share/nodejs/npm-package-arg/lib/npa.js on this box) checks a
    # version value against `isFilespec`
    # (`/^(?:[.]|~[/]|[/]|[a-zA-Z]:)/`) *before* it ever reaches the
    # HostedGit/slash fallback `_npm_looks_like_hosted_git_or_path` already
    # mirrors -- so a version value starting with a literal "." resolves
    # locally even with no "/" anywhere in it, not just multi-segment paths
    # like "../foo" (already caught by the "/" check). Confirmed live
    # end-to-end (npm 9.15.0, real `npm install`, no --dry-run): a scratch
    # package.json with `"totally-hallucinated-selfref-xyz-987": "."` in
    # devDependencies installed cleanly (node_modules/totally-hallucinated-
    # selfref-xyz-987 created, pointing back at the project's own
    # directory) with zero requests to registry.npmjs.org for that name in
    # a full --loglevel silly trace -- the only registry hit was the
    # unrelated bulk security-advisory POST every `npm install` makes.
    # Before this fix, slopcheck sent this name to the public npm registry
    # and would report it "NOT FOUND" -- a false positive on a dependency a
    # real `npm install` resolves entirely locally.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "devDependencies": {
                    "totally-hallucinated-selfref-xyz-987": ".",
                    "totally-hallucinated-parent-xyz-987": "..",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"react"}


def test_parse_package_json_skips_windows_drive_letter_local_path(tmp_path: Path):
    # `isFilespec`'s regex (`/^(?:[.]|~[/]|[/]|[a-zA-Z]:)/`, quoted in full in
    # the "." fix's own docstring above) has a *fourth* alternative besides
    # the leading-dot one that fix ported: a bare drive-letter prefix
    # (`[a-zA-Z]:`). It matches unconditionally on every platform -- unlike
    # npm-package-arg's `hasSlashes` (backslash only counts as a separator on
    # Windows), `isFilespec` has no such OS gate. Confirmed live (Linux, real
    # npm 9.2.0, `npm install --dry-run --loglevel silly`): a scratch
    # package.json with `"totally-hallucinated-name-xyz-123":
    # "C:\\Users\\dev\\local-lib"` made npm attempt
    # `open('/C:/Users/dev/local-lib/package.json')` and fail with ENOENT --
    # never a single request to registry.npmjs.org for that name. Same
    # result for a lowercase, no-backslash value ("c:foo" ->
    # open('.../c:foo/package.json')), confirming it's the bare `<letter>:`
    # prefix that triggers it. Before this fix, slopcheck sent both names to
    # the public npm registry and reported them "NOT FOUND" -- a false
    # positive on a real (if broken on this platform), locally-resolved
    # reference, the same false-positive shape as the "."/".." case just
    # for the regex's other alternative.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "totally-hallucinated-name-xyz-123": "C:\\Users\\dev\\local-lib",
                    "totally-hallucinated-name-xyz-456": "c:foo",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"react"}


def test_parse_package_json_still_checks_ambiguous_alpha_colon_version(tmp_path: Path):
    # A two-or-more-letter prefix before the colon does NOT match
    # `isFilespec`'s `[a-zA-Z]:` alternative (it requires exactly one letter
    # immediately before the colon) -- confirmed live (real npm 9.2.0,
    # `npm install --dry-run --loglevel silly` against
    # `"...": "AB:foo"`): npm never attempted a local file open the way it
    # does for a genuine single-letter drive prefix; it errored out via a
    # completely different code path (`isURL`'s generic `[a-z]+:` scheme
    # sniff -> EUNSUPPORTEDPROTOCOL for "ab:"), also never touching the
    # registry, but that's a distinct, broader gap (any alphabetic
    # "scheme:" prefix) outside this fix's scope. This test exists to pin
    # down the drive-letter regex's own boundary: it must require exactly
    # one leading letter, not any run of letters, or it would over-match
    # and skip real dependencies unnecessarily.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {
                    "totally-hallucinated-name-xyz-789": "AB:foo",
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"totally-hallucinated-name-xyz-789", "react"}


def test_parse_package_json_still_checks_yarn_patch_protocol_deps(tmp_path: Path):
    # Confirmed live (Yarn Berry 4.5.0, `yarn install` against a scratch
    # "patch:totally-hallucinated-name-xyz-987@npm%3A1.0.0#~/patches/fake.patch"
    # entry): Yarn genuinely queried the real npm registry mirror for the
    # named key and got a real 404, unlike the git-host-shorthand/local-path
    # forms the "/" heuristic above exists to catch. The exact shape here
    # (name + patch value) mirrors babel/babel's own real package.json,
    # which patches "@rollup/plugin-commonjs" and "rollup-plugin-dts" this
    # way. Without the patch: carve-out, the "/" in the patch file path
    # would make this indistinguishable from a git-host-shorthand value and
    # silently drop a real, checkable dependency.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "devDependencies": {
                    "@rollup/plugin-commonjs": (
                        "patch:@rollup/plugin-commonjs@npm%3A29.0.2"
                        "#~/.yarn/patches/@rollup-plugin-commonjs-npm-29.0.2-18c3a497d8.patch"
                    ),
                    "react": "^19.0.0",
                }
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"@rollup/plugin-commonjs", "react"}


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


def test_parse_package_json_npm_overrides_skips_non_registry_value(tmp_path: Path):
    # Confirmed live (npm 12.2.0): an overrides value can itself be a
    # file:/git-URL/tarball-URL specifier, not just a version/range/npm:
    # alias -- npm's own docs list "an exact version, a semver range, a
    # dist-tag, or a replacement specifier such as npm:, file:, or a Git
    # URL". `npm install --dry-run -v` against {"wrappy": "file:./local-
    # fork"} fetched only the other direct dependency from the registry,
    # never "wrappy" -- it's resolved straight from the path instead.
    # Before this fix, the override key was always checked against the
    # registry regardless of the value, so a legitimate file:/git: fork
    # override got reported as a hallucinated NOT FOUND package.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"once": "1.4.0"},
                "overrides": {
                    "wrappy": "file:./local-fork",
                    "semver": "git+https://github.com/npm/node-semver.git",
                    "real-override": "^1.0.0",
                },
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"once", "real-override"}
    assert "wrappy" not in names
    assert "semver" not in names


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


def test_parse_package_json_resolves_npm_alias_in_yarn_resolution_value(tmp_path: Path):
    # Confirmed live (Yarn Classic 1.22.22): a resolutions entry whose
    # *value* is an "npm:" alias substitutes a different real package for
    # the one the pattern's key names — `yarn install` genuinely queries the
    # registry for the alias target, not the key-derived name. Before this
    # fix, only the key was ever read, so an aliased entry like this one
    # checked "is-number" (a real, unrelated package) and never the actual
    # hallucinated name Yarn tries to fetch.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"is-odd": "^3.0.1"},
                "resolutions": {
                    "is-odd/**/is-number": "npm:totally-hallucinated-slopcheck-test-xyz-42@1.0.0",
                    "graceful-fs": "^4.2.11",
                },
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"is-odd", "totally-hallucinated-slopcheck-test-xyz-42", "graceful-fs"}
    assert "is-number" not in names


def test_parse_package_json_yarn_resolutions_skips_non_registry_value(tmp_path: Path):
    # Confirmed live (Yarn Classic 1.22.22): a resolutions value can also
    # be a file:/git-URL/tarball-URL specifier, resolved straight from
    # that location -- verbose yarn install log showed the registry was
    # never queried for "wrappy" under {"wrappy": "file:./local-fork"}.
    # Before this fix, the pattern's derived name was always checked
    # regardless of the value, so a legitimate fork override got reported
    # as a hallucinated package.
    pkg = tmp_path / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "dependencies": {"once": "1.4.0"},
                "resolutions": {
                    "wrappy": "file:./local-fork",
                    "graceful-fs": "^4.2.11",
                },
            }
        )
    )
    names = {dep.name for dep in parse_package_json(pkg)}
    assert names == {"once", "graceful-fs"}
    assert "wrappy" not in names


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


def test_parse_pyproject_poetry2_dependencies_overrides_pep621_source(tmp_path: Path):
    # Poetry 2.0+ (https://python-poetry.org/blog/announcing-poetry-2.0.0/)
    # makes [project.dependencies] the primary declaration and repurposes
    # [tool.poetry.dependencies] to attach a git/path/url source onto a
    # same-named entry already declared there — the exact shape
    # [tool.uv.sources] already gets special-cased for above, just for a
    # different, newer tool mechanism this parser didn't cross-reference at
    # all. Live-verified against real Poetry 2.5.1 (`poetry lock -vvv`): a
    # pyproject.toml with `[project] dependencies = ["requests"]` and
    # `[tool.poetry.dependencies] requests = {git = "..."}` made Poetry clone
    # the git repo for "requests" and never issue a single pypi.org request
    # for that name (pypi.org was hit only for requests' own transitive
    # deps: certifi, urllib3, idna, charset-normalizer). Before this fix,
    # `_pep621_deps` still emitted the bare "requests" string with nothing
    # to skip it, so a real git-sourced dependency (Poetry's own documented
    # pattern for pinning a fork of a PyPI package) was checked against
    # PyPI. "mkdocs" is a plain PEP 621 entry with no [tool.poetry.
    # dependencies] counterpart at all, confirming the override is scoped to
    # only the name it actually names, not a blanket "any [tool.poetry.
    # dependencies] present" skip.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests", "mkdocs"]

        [tool.poetry.dependencies]
        requests = { git = "https://example.com/requests-fork.git" }
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"mkdocs"}


def test_parse_pyproject_poetry_group_dependencies_overrides_dependency_groups_source(tmp_path: Path):
    # The identical enrichment shape as
    # test_parse_pyproject_poetry2_dependencies_overrides_pep621_source above,
    # one layer over: Poetry 2.0+ merges a [tool.poetry.group.<name>] table's
    # own `dependencies` into the *same-named* PEP 735 [dependency-groups]
    # entry by dependency name (poetry-core's
    # `Factory._configure_package_dependency_groups` ->
    # `DependencyGroup.dependencies_for_locking`), exactly mirroring how
    # [tool.poetry.dependencies] enriches [project.dependencies]'s MAIN
    # group. Live-verified against real Poetry 2.5.1 (`poetry lock -vvv`): a
    # pyproject.toml with `[dependency-groups] test =
    # ["totally-hallucinated-slopcheck-group-test-xyz-999"]` and
    # `[tool.poetry.group.test.dependencies] totally-hallucinated-slopcheck-
    # group-test-xyz-999 = { git = "file:///tmp/fakepkg" }` (a local repo
    # whose own pyproject.toml names that exact package, confirmed to 404 on
    # the real public PyPI JSON API) made `poetry lock` clone the git repo
    # and resolve it with zero requests to pypi.org. Before this fix,
    # `_dependency_groups_deps` still emitted the bare hallucinated name with
    # nothing to skip it (`_poetry_non_registry_names` only ever read
    # [tool.poetry.dependencies], never [tool.poetry.group.*]), so a real
    # git-sourced dependency-group entry was checked against PyPI and
    # reported `not_found`. "pytest" is a plain [dependency-groups] entry
    # with no [tool.poetry.group.test] counterpart at all, confirming the
    # override only drops the name it actually names.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = []

        [dependency-groups]
        test = ["totally-hallucinated-slopcheck-group-test-xyz-999", "pytest"]

        [tool.poetry.group.test.dependencies]
        totally-hallucinated-slopcheck-group-test-xyz-999 = { git = "https://example.com/fork.git" }
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"pytest"}


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


def test_parse_pyproject_pdm_dev_dependencies(tmp_path: Path):
    # PDM's own legacy `[tool.pdm.dev-dependencies]` table (predates PEP 735,
    # "Added in 1.5.0" per PDM's docs) — structurally identical to
    # [dependency-groups] (group name -> list of PEP 508 requirement strings)
    # but under a different, PDM-specific table this parser never read at
    # all. Confirmed live with real PDM 2.29.2 (`pdm lock -v` against exactly
    # this shape): it genuinely tried to resolve the fake name from PyPI
    # (CandidateNotFound), so a real `pdm install`/`pdm lock` installs
    # whatever's planted here just as much as a [dependency-groups] entry —
    # this table isn't deprecated or inert even though PDM's own `pdm add -dG`
    # now defaults to writing [dependency-groups] instead.
    # A group entry can also be a PDM-written editable/local/URL/VCS
    # dependency (confirmed live via `pdm add -e ./sub-package --dev`, which
    # wrote "-e file:///${PROJECT_ROOT}/sub-package#egg=subpkg" into this same
    # table) — that string must NOT be mistaken for a package name.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2"]

        [tool.pdm.dev-dependencies]
        test = ["pytest"]
        dev = [
            "-e file:///${PROJECT_ROOT}/sub-package#egg=subpkg",
        ]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "pytest"}


def test_parse_pyproject_uv_legacy_dev_dependencies(tmp_path: Path):
    # uv's own legacy `[tool.uv] dev-dependencies` list (predates PEP 735) —
    # a flat list of PEP 508 requirement strings, structurally identical to
    # [project.dependencies], under a sibling sub-key of [tool.uv] from the
    # already-read [tool.uv.sources]/[tool.uv.workspace]. Confirmed live
    # with real uv 0.12.19 (`uv lock -v` against exactly this shape): it
    # genuinely sent a GET to https://pypi.org/simple/<name>/ and failed
    # resolution ("was not found in the package registry") for the fake
    # name — uv prints a deprecation warning for this table but still
    # honors it, so a real `uv lock`/`uv sync` installs whatever's planted
    # here just as much as a [dependency-groups] entry. Before this fix,
    # parse_pyproject_toml returned [] against this exact file.
    #
    # A name here can still be [tool.uv.sources]-overridden exactly like a
    # [project.dependencies] entry (confirmed live: a `path =` source made
    # uv resolve the name from disk with zero PyPI requests) — the existing
    # skip_names filtering in parse_pyproject_toml already covers this for
    # any raw_specs contributor, so a sourced name here must NOT be
    # reported either.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2"]

        [tool.uv]
        dev-dependencies = [
            "totally-hallucinated-uv-legacy-devdep-xyz-842",
            "local-pkg",
        ]

        [tool.uv.sources]
        local-pkg = { path = "./local_pkg" }
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "totally-hallucinated-uv-legacy-devdep-xyz-842"}


def test_parse_pyproject_rye_dev_dependencies(tmp_path: Path):
    # Rye (https://rye.astral.sh/, largely merged into uv but still a real,
    # separately-installed tool) writes its own dev-only dependencies under
    # `[tool.rye] dev-dependencies` -- a flat list of PEP 508 requirement
    # strings, structurally identical to `[tool.uv] dev-dependencies` above
    # but under Rye's own table. Confirmed live with real Rye 0.44.0: `rye
    # sync -v` against exactly this shape genuinely sent a request to
    # https://pypi.org/simple/<name>/ and failed resolution ("was not found
    # in the package registry ... we can conclude that your requirements
    # are unsatisfiable"), aborting with "error: could not write dev
    # lockfile for project". Before this fix, nothing in this module read
    # `[tool.rye]` at all, so parse_pyproject_toml silently dropped the
    # hallucinated name and reported only `requests`/`hatchling`.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2"]

        [tool.rye]
        managed = true
        dev-dependencies = [
            "totally-hallucinated-rye-devdep-xyz-901",
        ]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "hatchling", "totally-hallucinated-rye-devdep-xyz-901"}


def test_parse_pyproject_rye_dev_dependencies_ignore_uv_sources_override(tmp_path: Path):
    # Unlike `[tool.uv] dev-dependencies`, a name in `[tool.rye]
    # dev-dependencies` is NOT skip_names-eligible via a same-named
    # `[tool.uv.sources]` local-path override, even though Rye delegates its
    # regular dependency resolution to uv under the hood. Confirmed live
    # (Rye 0.44.0): `rye sync -v`'s own debug log shows it materializes the
    # dev-dependencies list into a standalone temporary `requirements.txt`
    # and runs `uv compile` against *that* temp file directly ("Failed to
    # run uv compile /tmp/.../requirements.txt"), never against the
    # project's own pyproject.toml -- so the override table never enters
    # into it, and resolution still genuinely hit
    # https://pypi.org/simple/<name>/ and failed exactly like the plain
    # hallucinated-name case. A same-named [tool.uv.sources] path override
    # must therefore NOT suppress this report, unlike the `_uv_dev_deps`
    # case in test_parse_pyproject_uv_legacy_dev_dependencies above.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests>=2"]

        [tool.rye]
        managed = true
        dev-dependencies = [
            "totally-unique-local-rye-override-pkg-55214",
        ]

        [tool.uv.sources]
        totally-unique-local-rye-override-pkg-55214 = { path = "./localpkg" }
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert "totally-unique-local-rye-override-pkg-55214" in names


def test_parse_pyproject_hatch_env_dependencies(tmp_path: Path):
    # Hatch (the PyPA-recommended build backend/env manager) has two of its
    # own dependency-bearing tables, neither PEP 621 nor PEP 735: a plain
    # PEP 508 requirement-string list under each named
    # [tool.hatch.envs.<name>].dependencies, and a separate
    # [tool.hatch.env].requires listing environment-plugin packages Hatch
    # installs before it can even parse the rest of an environment's
    # config. Confirmed live with real Hatch 1.18.1 against a scratch
    # project: `hatch env create` genuinely tried (and failed) to resolve
    # a fake name from PyPI for both a
    # [tool.hatch.envs.default].dependencies entry ("Could not find a
    # version that satisfies the requirement ... (from versions: none)")
    # and a separate [tool.hatch.env].requires entry ("No solution found
    # when resolving dependencies ... was not found in the package
    # registry"). Before this fix neither table had any reader at all, so
    # a hallucinated name planted in either one sailed through unchecked.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests"]

        [tool.hatch.env]
        requires = ["totally-hallucinated-hatch-plugin-xyz-987"]

        [tool.hatch.envs.default]
        dependencies = ["totally-hallucinated-package-xyz-123"]

        [tool.hatch.envs.test]
        dependencies = ["pytest", "coverage[toml]"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {
        "requests",
        "totally-hallucinated-hatch-plugin-xyz-987",
        "totally-hallucinated-package-xyz-123",
        "pytest",
        "coverage",
    }


def test_parse_pyproject_hatch_env_extra_dependencies(tmp_path: Path):
    # Hatch's own `extra-dependencies` field
    # (https://hatch.pypa.io/latest/config/environment/overview/#dependencies)
    # lets an environment that inherits from another (implicitly from
    # `default`, unless a different `template` is set) add packages on top
    # of the inherited `dependencies` list without redeclaring it. It's an
    # ordinary field on the same [tool.hatch.envs.<name>] table
    # `dependencies` already lives on, resolved by Hatch's own
    # `environment_dependencies_complex` through the identical validation
    # and install path as `dependencies` -- not a separate, rarer
    # mechanism. Confirmed live with real Hatch 1.18.1 against a scratch
    # project: `hatch env create experimental` for exactly this shape
    # genuinely failed resolving the fake name from PyPI ("Could not find
    # a version that satisfies the requirement
    # totally-hallucinated-hatch-extradep-xyz-123 (from versions: none)").
    # Before this fix, `_hatch_deps` only ever read `dependencies`, so this
    # sibling field was silently never checked at all.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests"]

        [tool.hatch.envs.default]
        dependencies = ["pytest"]

        [tool.hatch.envs.experimental]
        extra-dependencies = ["totally-hallucinated-hatch-extradep-xyz-123"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {
        "requests",
        "pytest",
        "totally-hallucinated-hatch-extradep-xyz-123",
    }


def test_parse_pyproject_hatch_env_overrides_dependencies(tmp_path: Path):
    # Hatch has a *third* way to inject dependencies into a named
    # environment, on top of the plain `dependencies`/`extra-dependencies`
    # fields the two tests above already cover: an `overrides` sub-table
    # (https://hatch.pypa.io/latest/config/environment/advanced/#overrides)
    # of the form `overrides.<source>.<condition>.<option>`, where `source`
    # is one of platform/env/matrix/name and `option` can itself be
    # `dependencies`/`extra-dependencies`. Each entry is either a plain
    # string or a `{value = "...", if = [...]}` table gating it on a
    # specific matrix value (or platform/env-var/env-name) match.
    #
    # Confirmed live with real Hatch 1.18.1 against a scratch project using
    # exactly the shape below: `hatch env create test.a` genuinely tried
    # (and failed) to resolve the fake name from PyPI ("Could not find a
    # version that satisfies the requirement
    # totally-hallucinated-hatch-override-xyz-999 (from versions: none)"),
    # while `hatch env create test.b` (the matrix variant whose `if`
    # condition doesn't match) installed only `requests` -- confirming the
    # entry really is condition-gated at runtime, and that slopcheck
    # deliberately does not try to evaluate that gating itself (it has no
    # reliable notion of which platform/env/matrix variant a real `hatch`
    # invocation will use, and the one that *did* match above was still a
    # real, installable dependency). Before this fix, `_hatch_deps` never
    # read `overrides` at all, so this name was silently never checked.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests"]

        [tool.hatch.envs.test]
        dependencies = ["requests"]

        [[tool.hatch.envs.test.matrix]]
        pyver = ["a", "b"]

        [tool.hatch.envs.test.overrides]
        matrix.pyver.dependencies = [
            { value = "totally-hallucinated-hatch-override-xyz-999", if = ["a"] },
        ]
        platform.linux.extra-dependencies = ["totally-hallucinated-hatch-override-platform-xyz-1"]
        name.test.dependencies = ["totally-hallucinated-hatch-override-name-xyz-2"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {
        "requests",
        "totally-hallucinated-hatch-override-xyz-999",
        "totally-hallucinated-hatch-override-platform-xyz-1",
        "totally-hallucinated-hatch-override-name-xyz-2",
    }


def test_parse_pyproject_hatch_build_dependencies(tmp_path: Path):
    # Hatch has a *second*, entirely separate family of dependency fields
    # from the three [tool.hatch.env]/[tool.hatch.envs.*] tests above:
    # [tool.hatch.build] and its sub-tables, read by hatchling's own build
    # backend (not its environment manager) when computing the PEP 517
    # isolated-build-environment requirements, analogous to [build-system]
    # requires but computed dynamically rather than declared statically.
    # Read directly from installed hatchling 1.28's `builders/config.py`
    # (`BuilderConfig.dependencies`): it merges
    # [tool.hatch.build].dependencies (global),
    # [tool.hatch.build.targets.<target>].dependencies (per build target),
    # [tool.hatch.build.hooks.<hook>].dependencies (global build hooks), and
    # [tool.hatch.build.targets.<target>.hooks.<hook>].dependencies
    # (target-specific build hooks).
    #
    # Confirmed live with real hatchling 1.28 (via pip) against a scratch
    # project with [build-system] requires = ["hatchling"] and
    # [tool.hatch.build] dependencies = ["totally-hallucinated-hatch-build-
    # dep-xyz-741"]: `pip install .` ran "Installing backend dependencies"
    # as its own step (after "Getting requirements to build wheel" already
    # succeeded) and genuinely failed resolving the fake name from PyPI
    # ("Could not find a version that satisfies the requirement ... (from
    # versions: none)"). Before this fix, nothing in this module read
    # [tool.hatch.build] at all (only [tool.hatch.env]/[tool.hatch.envs.*]
    # were read), so a hallucinated name planted in any of its four
    # dependency-bearing sub-fields was silently never checked even though
    # a real `pip install .`/`pip wheel .`/`python -m build` genuinely
    # tries to resolve it before the build even starts.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = ["requests"]

        [tool.hatch.build]
        dependencies = ["totally-hallucinated-hatch-build-dep-xyz-741"]

        [tool.hatch.build.hooks.custom]
        dependencies = ["totally-hallucinated-hatch-build-hook-dep-xyz-2"]

        [tool.hatch.build.targets.wheel]
        dependencies = ["totally-hallucinated-hatch-build-target-dep-xyz-3"]

        [tool.hatch.build.targets.wheel.hooks.vcs]
        dependencies = ["totally-hallucinated-hatch-build-target-hook-dep-xyz-4"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {
        "requests",
        "totally-hallucinated-hatch-build-dep-xyz-741",
        "totally-hallucinated-hatch-build-hook-dep-xyz-2",
        "totally-hallucinated-hatch-build-target-dep-xyz-3",
        "totally-hallucinated-hatch-build-target-hook-dep-xyz-4",
    }


def test_parse_pyproject_build_system_requires(tmp_path: Path):
    # PEP 518 makes [build-system] requires mandatory for any project pip
    # can build from source — a plain list of PEP 508 requirement strings
    # naming real PyPI packages pip installs into an isolated build
    # environment *before* running the build, completely independent of
    # [project.dependencies]. Confirmed real and current against numpy's
    # actual pyproject.toml: [build-system] requires =
    # ["meson-python>=0.20.0", "Cython>=3.1.0"] — neither name appears
    # anywhere under [project]. Before this fix, this table had no reader
    # at all: a hallucinated name planted only here (a real place for one
    # to end up, since an AI assistant asked to scaffold a custom build
    # backend can invent this list the same way it can invent a runtime
    # dependency) was silently never checked, even though a real `pip
    # install` from source genuinely installs it first.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [build-system]
        requires = ["meson-python>=0.20.0", "Cython>=3.1.0"]
        build-backend = "mesonpy"

        [project]
        name = "demo"
        dependencies = ["requests"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "meson-python", "Cython"}


def test_parse_pyproject_non_string_build_system_requires_raises_clean_error(tmp_path: Path):
    # Real, live pip test fixture: pypa/pip's own repo ships
    # tests/data/src/pep518_invalid_requires/pyproject.toml with exactly
    # this shape (`requires = [1, 2, 3]  # not a list of strings`), and
    # scanning pip's own checkout crashed slopcheck outright with
    # `AttributeError: 'int' object has no attribute 'strip'` instead of a
    # clean error — real `pip install` on a project with this table Fatals
    # immediately ("error: invalid-pyproject-build-system-requires", "It is
    # not a list of strings") before resolving anything, so a clean
    # ManifestParseError (not a crash, not a silent skip) is the correct
    # outcome here, mirroring how this module already reports any other
    # unparseable manifest.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [build-system]
        requires = [1, 2, 3]
        """
    )
    try:
        parse_pyproject_toml(pyproject)
        assert False, "expected ManifestParseError"
    except ManifestParseError as e:
        assert "pyproject.toml" in str(e)


def test_parse_pyproject_non_string_project_dependency_raises_clean_error(tmp_path: Path):
    # Same crash shape, reached through [project.dependencies] instead of
    # [build-system] requires. Confirmed live: setuptools' own
    # pyproject.toml reader Fatals the same way ("configuration error:
    # 'project.dependencies[0]' must be string") during the build-requires
    # subprocess a real `pip install` spawns.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = [1, 2]
        """
    )
    try:
        parse_pyproject_toml(pyproject)
        assert False, "expected ManifestParseError"
    except ManifestParseError as e:
        assert "pyproject.toml" in str(e)


def test_parse_pyproject_uv_sources_skip_does_not_leak_into_build_system_or_hatch(
    tmp_path: Path,
):
    # [tool.uv.sources]'s git/path/workspace/url skip exists for one reason:
    # those protocols mean the *regular* dependency resolver (uv, reading
    # [project.dependencies]/[dependency-groups]) won't ask the index for
    # that name at all. [build-system] requires and Hatch's own
    # [tool.hatch.env]/[tool.hatch.envs.*] tables are resolved by entirely
    # separate mechanisms — a PEP 517 isolated build environment (plain pip,
    # with zero knowledge of uv-specific config) and Hatch's own env
    # manager, respectively — neither of which consults [tool.uv.sources] at
    # all. Before this fix, parse_pyproject_toml built one combined
    # raw_specs list from every table (PEP 621, Poetry, dependency-groups,
    # PDM, *and* build-system/Hatch) and ran the *same* skip_names set
    # (derived only from [tool.uv.sources]/self-referential-extra rules)
    # over the whole thing, so a name that happened to also appear in
    # [build-system] requires or a Hatch env table got silently skipped too,
    # purely by name collision with an unrelated uv.sources entry.
    #
    # Confirmed live (this box, venv + pip 25.x): a pyproject.toml with
    # `[tool.uv.sources] totally-hallucinated-buildreq-xyz-123 = { path =
    # "./local-pkg" }` and `[build-system] requires = ["setuptools",
    # "totally-hallucinated-buildreq-xyz-123"]` made `pip install .`
    # genuinely try to fetch "totally-hallucinated-buildreq-xyz-123" from
    # PyPI while installing build dependencies (pip's build-isolation step
    # never parses [tool.uv.sources] — that table is uv-specific, not a PEP
    # 517/518 concept) and fail with "Could not find a version that
    # satisfies the requirement ... (from versions: none)". Before this
    # fix, slopcheck against the same file reported "2 dependencies checked,
    # all clean" (setuptools + requests only) — a silent false negative on
    # exactly the fabricated name real pip tries and fails to install.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo-project"
        dependencies = ["requests"]

        [tool.uv.sources]
        totally-hallucinated-buildreq-xyz-123 = { path = "./local-pkg" }
        totally-hallucinated-hatch-plugin-xyz-987 = { workspace = true }

        [build-system]
        requires = ["setuptools", "totally-hallucinated-buildreq-xyz-123"]
        build-backend = "setuptools.build_meta"

        [tool.hatch.env]
        requires = ["totally-hallucinated-hatch-plugin-xyz-987"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {
        "requests",
        "setuptools",
        "totally-hallucinated-buildreq-xyz-123",
        "totally-hallucinated-hatch-plugin-xyz-987",
    }


def test_parse_pyproject_skips_self_referential_extras(tmp_path: Path):
    # PEP 621 explicitly supports a "self-referential extra": an entry in
    # [project.optional-dependencies] (or [dependency-groups]) that names
    # the *current* project with a different combination of its own extras
    # instead of an external dependency, so an "all"/"everything" extra
    # doesn't need a hand-maintained copy of every other extra
    # (https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#self-referential-extras).
    # Confirmed real and current against PDM's own pyproject.toml, which
    # does exactly this shape: [project.optional-dependencies] template =
    # ["pdm[copier,cookiecutter]"] / all = ["pdm[keyring,template]"], and
    # [dependency-groups] test = ["pdm[pytest]", ...] — naming "pdm" itself
    # in three different extras/groups, not an external dependency. Before
    # this fix the self-reference matched _REQ_LINE_RE like any other spec
    # and got checked against PyPI as if it were a real dependency — a
    # spurious "not found"/"recent" flag waiting to happen for the exact
    # case this pattern exists for: a brand-new, not-yet-published project
    # using its own umbrella extra during early development. The project
    # name is matched PEP 503-normalized (case/separator-insensitive,
    # "Demo-Tool" vs "demo_tool") since [project.name] is itself normalized
    # that way.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "Demo-Tool"
        dependencies = ["requests"]

        [project.optional-dependencies]
        gui = ["PyQt5"]
        cli = ["click"]
        all = ["demo_tool[gui,cli]"]

        [dependency-groups]
        test = ["pytest", "demo-tool[cli]"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"requests", "PyQt5", "click", "pytest"}


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


def test_parse_pyproject_setuptools_dynamic_does_not_follow_pip_directives(tmp_path: Path):
    # Same underlying bug as
    # test_parse_setup_cfg_file_directive_does_not_follow_pip_directives,
    # found in the sibling PEP 621 `[tool.setuptools.dynamic]` code path --
    # confirmed (reading pyprojecttoml.py's `_expand_directive`) to bottom
    # out in the identical `expand.read_files` + flat-split logic as
    # setup.cfg's own `file:` directive, and confirmed live that a real `pip
    # install .` against this exact shape Fatals with the same
    # `InvalidRequirement` error before resolving anything. Before this fix,
    # `_setuptools_dynamic_deps` read the referenced file with
    # `parse_requirements_txt`, which genuinely follows the "-r" line.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dynamic = ["dependencies"]

        [tool.setuptools.dynamic]
        dependencies = { file = "base-reqs.txt" }
        """
    )
    (tmp_path / "base-reqs.txt").write_text("-r inner-reqs.txt\nflask\n")
    (tmp_path / "inner-reqs.txt").write_text("totally-hallucinated-dynamicdep-xyz-888\n")
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"flask"}
    assert "totally-hallucinated-dynamicdep-xyz-888" not in names


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


def test_find_manifests_prunes_non_dotted_virtualenv_directory_names(tmp_path: Path):
    # A virtualenv isn't always dot-prefixed: Python's own venv module docs
    # say environments are "conventionally named `.venv` or `venv`", and
    # GitHub's official `Python.gitignore` template (what `gh repo create
    # --gitignore Python`/the GitHub web UI write into new repos) lists
    # `env/`, `venv/`, `ENV/`, `env.bak/`, and `venv.bak/` as equally-real
    # virtualenv directory names alongside `.venv`. Confirmed live: a fresh
    # `venv` (no dot) with pandas installed carries pandas' own
    # `pyproject.toml` (90 raw dependency specs unrelated to the project
    # being scanned) under `venv/lib/.../site-packages/pandas/`, the exact
    # same false-signal shape the dot-prefixed `.venv` case above is already
    # pruned for.
    (tmp_path / "requirements.txt").write_text("requests\n")
    for dirname in ("venv", "env", "ENV", "venv.bak", "env.bak"):
        installed = tmp_path / dirname / "lib" / "site-packages" / "pandas"
        installed.mkdir(parents=True)
        (installed / "pyproject.toml").write_text('[project]\nname = "pandas"\ndependencies = ["numpy"]\n')

    found = {str(p.relative_to(tmp_path)) for p in find_manifests(tmp_path)}

    assert found == {"requirements.txt"}


def test_find_manifests_prunes_pdm_pypackages_directory(tmp_path: Path):
    # PDM has a third, independent local-install mechanism with the same
    # "contains already-installed packages' own manifests" shape as
    # node_modules/venv above: PEP 582's `__pypackages__` directory, which
    # `pdm install`/`pdm add` populate by default whenever
    # `python.use_venv` is off (a real, current PDM mode, not deprecated)
    # instead of creating any virtualenv — so neither the dot-prefix rule
    # nor the venv/env-name rule prunes it. Confirmed live: real PDM 2.29.2
    # with `python.use_venv = false`, `pdm add pandas` writes
    # `__pypackages__/3.13/lib/pandas/pyproject.toml` (pandas' own ~90-entry
    # dependency spec) into a project whose own pyproject.toml declares only
    # `requests`/`pandas` — before this fix, scanning the project root
    # reported 47 dependency results instead of 2, almost all of them
    # pandas' own optional/test/doc dependencies unrelated to the project
    # actually being scanned.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pdmtest"\ndependencies = ["requests", "pandas"]\n'
    )
    installed = tmp_path / "__pypackages__" / "3.13" / "lib" / "pandas"
    installed.mkdir(parents=True)
    (installed / "pyproject.toml").write_text(
        '[project]\nname = "pandas"\ndependencies = ["numpy", "python-dateutil", "pytz"]\n'
    )

    found = {str(p.relative_to(tmp_path)) for p in find_manifests(tmp_path)}

    assert found == {"pyproject.toml"}


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


def test_parse_requirements_txt_recurses_into_no_space_short_flag(tmp_path: Path):
    # pip's requirements-file option parser is a plain `optparse.OptionParser`
    # (`pip._internal.req.req_file.SUPPORTED_OPTIONS`), which accepts a short
    # option's argument concatenated with no separator at all, not just
    # space-separated — live-verified directly against installed pip 25.1.1
    # (`pip._internal.req.req_file.parse_requirements`): a nested file named
    # via "-rbase.txt" (no space) genuinely resolved and recursed into
    # base.txt, identically to "-r base.txt". Before this fix, the regex
    # required literal whitespace after "-r", so this real, pip-accepted
    # form fell through unmatched and the nested file's hallucinated
    # dependency was never checked at all.
    (tmp_path / "base.txt").write_text("totally-fake-hallucinated-pkg==1.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text("-rbase.txt\nflask\n")
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"totally-fake-hallucinated-pkg", "flask"}


def test_parse_requirements_txt_recurses_into_equals_form_long_flag(tmp_path: Path):
    # Same optparse gap as the no-space short-flag case above, for the long
    # option's "=" form instead: live-verified against real pip 25.1.1 that
    # "--requirement=base.txt" genuinely resolves and recurses into
    # base.txt, identically to "--requirement base.txt".
    (tmp_path / "base.txt").write_text("totally-fake-hallucinated-pkg==1.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text("--requirement=base.txt\nflask\n")
    names = {dep.name for dep in parse_requirements_txt(req)}
    assert names == {"totally-fake-hallucinated-pkg", "flask"}


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


def test_parse_requirements_txt_no_space_constraint_flag_not_treated_as_dependency(tmp_path: Path):
    # Same no-space-separator form as "-rbase.txt" above, for "-c" instead —
    # live-verified against real pip 25.1.1 that "-cbase.txt" (no space)
    # resolves identically to "-c base.txt", i.e. as a constraints
    # directive, not a package name. Guards against a regression where
    # widening _REQ_FILE_RE's separator handling accidentally makes this
    # line match as a nested -r/--requirement recursion instead of staying
    # a recognized, skipped constraints directive.
    (tmp_path / "constraints.txt").write_text("only-a-version-pin==1.0\n")
    req = tmp_path / "requirements.txt"
    req.write_text("-cconstraints.txt\nrequests\n")
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


def test_find_manifests_discovers_requirements_in(tmp_path: Path):
    # pip-tools' own convention: a hand-edited "requirements.in" compiled by
    # `pip-compile` into "requirements.txt" -- the same requirements-file
    # syntax, just a different, equally conventional filename `find_manifests`
    # didn't recognize at all before this fix.
    (tmp_path / "requirements.in").write_text("requests\n")
    found = {p.name for p in find_manifests(tmp_path)}
    assert "requirements.in" in found


def test_parse_manifest_routes_requirements_in_to_requirements_parser(tmp_path: Path):
    req = tmp_path / "requirements.in"
    req.write_text("requests\ntotally-hallucinated-package-xyz-123\n")
    names = {dep.name for dep in parse_manifest(req)}
    assert names == {"requests", "totally-hallucinated-package-xyz-123"}


def test_parse_manifest_routes_non_canonical_in_filename_to_requirements_parser(tmp_path: Path):
    # Same "any file with this suffix is real-world routine" gap already
    # fixed for non-canonical ".txt" names (see the sibling test above) --
    # pip-tools projects split the same way (e.g. requirements/base.in,
    # dev.in), and pip-compile's own parser (pip._internal.req.req_file.
    # parse_requirements, the exact function real pip itself uses) doesn't
    # care about the filename at all, only this convention does.
    req = tmp_path / "requirements-dev.in"
    req.write_text("pytest==8.0.0\n")
    names = {dep.name for dep in parse_manifest(req)}
    assert names == {"pytest"}


def test_find_manifests_discovers_non_canonical_requirements_filenames(tmp_path: Path):
    # `parse_manifest` was fixed (see the two tests above) to route any
    # ".txt"/".in" file to the requirements parser when given a direct path
    # -- but `find_manifests`, the directory-walk function the tool's own
    # default/documented invocation (`slopcheck` with no args, scanning the
    # current directory) relies on, only ever looks for the exact literal
    # filenames "requirements.txt"/"requirements.in" (PARSERS' own keys). It
    # never applies that same ".txt"/".in" fallback during the walk, so a
    # real project's split requirements files are never even found, let
    # alone parsed, unless a user names them individually on the command
    # line. Two real, currently-live examples confirm this isn't
    # contrived: home-assistant/core's root carries "requirements.txt"
    # (production deps) alongside a sibling "requirements_test.txt" (real
    # pinned test/lint deps -- mypy, astroid, coverage, etc. -- confirmed
    # against the actual file on github.com/home-assistant/core), with
    # neither file "-r"-including the other; cookiecutter-django's
    # generated project has no top-level requirements.txt at all, only
    # "requirements/base.txt", "requirements/local.txt", and
    # "requirements/production.txt" (confirmed against the actual template
    # on github.com/cookiecutter/cookiecutter-django). `pip install -r
    # requirements_test.txt`/`pip install -r requirements/base.txt` are
    # both perfectly ordinary, real pip invocations -- pip never cares
    # what a requirements file is named, only this tool's own directory-
    # walk discovery does.
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "requirements_test.txt").write_text("totally-hallucinated-ha-test-dep-xyz-123\n")
    nested = tmp_path / "requirements"
    nested.mkdir()
    (nested / "base.txt").write_text("totally-hallucinated-ccd-base-dep-xyz-456\n")

    found = {str(p.relative_to(tmp_path)) for p in find_manifests(tmp_path)}

    assert found == {
        "requirements.txt",
        "requirements_test.txt",
        str(Path("requirements", "base.txt")),
    }


def test_find_manifests_discovers_pixi_toml(tmp_path: Path):
    (tmp_path / "pixi.toml").write_text(
        '[workspace]\nchannels = ["conda-forge"]\nplatforms = ["linux-64"]\n'
        '\n[pypi-dependencies]\nrequests = "*"\n'
    )
    found = {p.name for p in find_manifests(tmp_path)}
    assert "pixi.toml" in found


def test_parse_manifest_routes_pixi_toml_to_pixi_parser(tmp_path: Path):
    pixi = tmp_path / "pixi.toml"
    pixi.write_text('[pypi-dependencies]\nrequests = "*"\n')
    names = {dep.name for dep in parse_manifest(pixi)}
    assert names == {"requests"}


def test_parse_pixi_toml(tmp_path: Path):
    # Real-world find: pixi (https://pixi.sh, an actively-developed
    # conda+PyPI package manager from prefix.dev) had no PARSERS/
    # find_manifests entry at all — a pixi-only project's entire PyPI
    # dependency list, hallucinated names included, was silently never
    # scanned, both via directory discovery ("slopcheck: no requirements.txt,
    # ... found") and when pointed at the file directly
    # (ManifestParseError's "don't know how to parse this file"). Confirmed
    # live with real pixi 0.81.0 (`pixi init . --format pixi`, `pixi add
    # python`, `pixi add --pypi requests`, then a hand-added hallucinated
    # entry): `pixi install` genuinely resolved `requests` from PyPI and
    # failed on the fake name ("was not found in the package registry").
    #
    # `[dependencies]` (conda-channel packages, e.g. the real `python`
    # entry pixi itself writes) must NOT be checked against PyPI — the
    # same reasoning already applied to environment.yml's own conda
    # `dependencies:` list. A `pypi-dependencies` entry whose value is a
    # table with `path`/`git`/`url` (confirmed live: `pixi add --pypi
    # --path ./localpkg localpkg` wrote `localpkg = { path = "localpkg" }`)
    # resolves from disk/a VCS remote, not PyPI, and must be excluded the
    # same way Poetry's/uv's own git/path/url table forms already are.
    # `[feature.<name>.pypi-dependencies]` (confirmed live: `pixi add
    # --pypi --feature test pytest` wrote a real, separately-resolved
    # table even before the feature is wired into any environment) is a
    # second, independent source of real PyPI names.
    pixi = tmp_path / "pixi.toml"
    pixi.write_text(
        """
        [workspace]
        channels = ["conda-forge"]
        platforms = ["linux-64"]

        [dependencies]
        python = ">=3.14,<3.15"

        [pypi-dependencies]
        requests = ">=2.34.2, <3"
        totally-hallucinated-pixi-pypi-xyz-789 = "*"
        localpkg = { path = "localpkg" }

        [feature.test.pypi-dependencies]
        pytest = "*"
        """
    )
    names = {dep.name for dep in parse_pixi_toml(pixi)}
    assert names == {"requests", "totally-hallucinated-pixi-pypi-xyz-789", "pytest"}


def test_parse_pyproject_toml_reads_tool_pixi_pypi_dependencies(tmp_path: Path):
    # pixi's own documented alternative to a standalone pixi.toml: identical
    # tables embedded under a pyproject.toml's [tool.pixi]. Confirmed live
    # (pixi 0.81.0): a hand-written [tool.pixi.pypi-dependencies] entry with
    # no corresponding [project.dependencies] entry made a real `pixi
    # install` fail resolving the fake name from PyPI exactly like the
    # standalone-pixi.toml case — before this fix, parse_pyproject_toml
    # never read [tool.pixi] at all, so this table's names (unlike pixi's
    # own default behavior of mirroring default-feature pypi-dependencies
    # into [project.dependencies], which parse_pyproject_toml already
    # covered via _pep621_deps) were silently never checked.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = []

        [tool.pixi.workspace]
        channels = ["conda-forge"]

        [tool.pixi.dependencies]
        python = ">=3.11"

        [tool.pixi.pypi-dependencies]
        totally-hallucinated-pixi-pyproject-xyz-555 = "*"

        [tool.pixi.feature.test.pypi-dependencies]
        pytest = "*"
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"totally-hallucinated-pixi-pyproject-xyz-555", "pytest"}


def test_find_manifests_discovers_tox_ini(tmp_path: Path):
    (tmp_path / "tox.ini").write_text("[testenv]\ndeps =\n    pytest\n")
    found = {p.name for p in find_manifests(tmp_path)}
    assert "tox.ini" in found


def test_parse_manifest_routes_tox_ini_to_tox_parser(tmp_path: Path):
    tox_ini = tmp_path / "tox.ini"
    tox_ini.write_text("[testenv]\ndeps =\n    pytest\n")
    names = {dep.name for dep in parse_manifest(tox_ini)}
    assert names == {"pytest"}


def test_parse_tox_ini(tmp_path: Path):
    # Real-world find: tox (https://tox.wiki/, a real, current, widely-used
    # Python test-automation tool) had no PARSERS/find_manifests entry at
    # all — a project's real test/lint dependencies, hallucinated names
    # included, were silently never scanned, both via directory discovery
    # and when `tox.ini` was pointed at directly ("don't know how to parse
    # this file"). Confirmed live with real tox 4.64.8: `tox -e py313`
    # against a [testenv] `deps` list containing a hallucinated name
    # genuinely ran `python -I -m pip install pytest
    # totally-hallucinated-tox-testdep-xyz-456` and failed with pip's real
    # "Could not find a version that satisfies the requirement ... (from
    # versions: none)".
    #
    # Every `[testenv:name]` section is read independently, not just the
    # bare `[testenv]` default — a hallucinated name in a lint-only
    # environment's `deps` is just as real a dependency as one in the
    # default environment's. A tox factor-conditional prefix ("py39:
    # pytest-cov", https://tox.wiki/en/latest/config.html#factor-
    # conditional-settings) is stripped so the package name underneath is
    # still recognized. A line using tox's own `{[testenv]deps}`
    # cross-section-reference substitution (which this parser doesn't
    # expand — see _tox_env_deps_lines's own docstring for why) is simply
    # skipped rather than mistaken for a literal package name.
    tox_ini = tmp_path / "tox.ini"
    tox_ini.write_text(
        """
        [tox]
        envlist = py313, lint

        [testenv]
        deps =
            pytest
            totally-hallucinated-tox-base-dep-111

        [testenv:lint]
        deps =
            {[testenv]deps}
            ruff
            totally-hallucinated-tox-lint-dep-222

        [testenv:factors]
        deps =
            py39,py310: pytest-cov
            totally-hallucinated-tox-factor-dep-333
        """
    )
    names = {dep.name for dep in parse_tox_ini(tox_ini)}
    assert names == {
        "pytest",
        "totally-hallucinated-tox-base-dep-111",
        "ruff",
        "totally-hallucinated-tox-lint-dep-222",
        "pytest-cov",
        "totally-hallucinated-tox-factor-dep-333",
    }


def test_parse_tox_ini_ignores_unresolvable_requirement_file_reference(tmp_path: Path):
    # tox's own substitution syntax (`{toxinidir}`, `{envname}`, ...) is
    # routinely used inside a `-r`/`-c` target (e.g.
    # `-r{toxinidir}/requirements-dev.txt`), which this parser doesn't
    # implement. Naively joining tox.ini's own directory with the
    # still-literal "{toxinidir}/requirements-dev.txt" text would raise a
    # confusing ManifestParseError ("referenced requirements file not
    # found") for a perfectly ordinary tox.ini, so a `-r`/`-c`-prefixed
    # line is deliberately left unresolved rather than guessed at — this
    # must not raise, and must not be mistaken for a plain package name.
    tox_ini = tmp_path / "tox.ini"
    tox_ini.write_text(
        """
        [testenv]
        deps =
            -r{toxinidir}/requirements-dev.txt
            pytest
        """
    )
    names = {dep.name for dep in parse_tox_ini(tox_ini)}
    assert names == {"pytest"}


def test_find_manifests_discovers_tox_toml(tmp_path: Path):
    (tmp_path / "tox.toml").write_text('[env_run_base]\ndeps = ["pytest"]\n')
    found = {p.name for p in find_manifests(tmp_path)}
    assert "tox.toml" in found


def test_parse_manifest_routes_tox_toml_to_tox_toml_parser(tmp_path: Path):
    tox_toml = tmp_path / "tox.toml"
    tox_toml.write_text('[env_run_base]\ndeps = ["pytest"]\n')
    names = {dep.name for dep in parse_manifest(tox_toml)}
    assert names == {"pytest"}


def test_parse_tox_toml(tmp_path: Path):
    # Real-world find: since tox 4.21 (https://tox.wiki/en/latest/config.html#toml-based-configuration),
    # tox has a second, completely independent config format: native TOML,
    # either in a standalone tox.toml or embedded in pyproject.toml's
    # [tool.tox] (see test_parse_pyproject_toml_reads_tool_tox_env_deps
    # below) -- neither was read by this tool at all before this fix.
    # hukkin/mdformat's own real, current pyproject.toml configures tox
    # entirely this way, with no tox.ini anywhere. Confirmed live with
    # real tox 4.64.9: a `[env.py313] deps = ["pytest",
    # "totally-hallucinated-slopcheck-tox-native-xyz-999"]` table made
    # `tox -e py313` genuinely run `python -I -m pip install pytest
    # totally-hallucinated-slopcheck-tox-native-xyz-999` and fail with
    # pip's real "Could not find a version that satisfies the requirement
    # ... (from versions: none)"; a `[env_base.unit] factors = ["py313"]`
    # factor-template table (used by multiple generated env names sharing
    # one deps list) resolves identically under `tox -e unit-py313`.
    tox_toml = tmp_path / "tox.toml"
    tox_toml.write_text(
        """
        env_list = ["py313"]

        [env_run_base]
        deps = ["pytest", "totally-hallucinated-tox-toml-base-111"]

        [env_pkg_base]
        deps = ["setuptools", "totally-hallucinated-tox-toml-pkgbase-222"]

        [env.py313]
        deps = [
            "{[env_run_base]deps}",
            "ruff",
            "totally-hallucinated-tox-toml-env-333",
        ]

        [env_base.unit]
        factors = ["py313"]
        deps = ["totally-hallucinated-tox-toml-envbase-444"]

        [env.docs]
        deps = []
        """
    )
    names = {dep.name for dep in parse_tox_toml(tox_toml)}
    assert names == {
        "pytest",
        "totally-hallucinated-tox-toml-base-111",
        "setuptools",
        "totally-hallucinated-tox-toml-pkgbase-222",
        "ruff",
        "totally-hallucinated-tox-toml-env-333",
        "totally-hallucinated-tox-toml-envbase-444",
    }


def test_parse_tox_toml_ignores_non_string_deps_entry(tmp_path: Path):
    # tox's own JSON schema (tox/tox.schema.json) allows a `deps` array
    # item to be a `{replace = ...}`-style substitution-extension table
    # instead of a plain string (confirmed real in mdformat's own
    # `commands` usage of the identical extension). Must not crash, and
    # must not be mistaken for a literal package name.
    tox_toml = tmp_path / "tox.toml"
    tox_toml.write_text(
        """
        [env.py313]
        deps = [
            "pytest",
            { replace = "posargs", default = ["mypy"], extend = true },
        ]
        """
    )
    names = {dep.name for dep in parse_tox_toml(tox_toml)}
    assert names == {"pytest"}


def test_parse_pyproject_toml_reads_tool_tox_env_deps(tmp_path: Path):
    # tox's own documented alternative to a standalone tox.toml: identical
    # tables embedded under a pyproject.toml's [tool.tox] -- hukkin/mdformat's
    # actual real pyproject.toml uses exactly this form for its whole tox
    # setup. Confirmed live (tox 4.64.9): a `[tool.tox.env.py313] deps`
    # entry with no [project.dependencies] equivalent made a real `tox -e
    # py313` genuinely fail resolving the hallucinated name from PyPI --
    # before this fix, parse_pyproject_toml never read [tool.tox] at all.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo"
        dependencies = []

        [tool.tox]
        env_list = ["py313"]

        [tool.tox.env_run_base]
        deps = ["pytest"]

        [tool.tox.env."py313"]
        deps = ["totally-hallucinated-slopcheck-tox-native-xyz-999"]
        """
    )
    names = {dep.name for dep in parse_pyproject_toml(pyproject)}
    assert names == {"pytest", "totally-hallucinated-slopcheck-tox-native-xyz-999"}


def test_find_manifests_discovers_pre_commit_config(tmp_path: Path):
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n-   repo: foo\n    hooks:\n    -   id: mypy\n"
    )
    found = {p.name for p in find_manifests(tmp_path)}
    assert ".pre-commit-config.yaml" in found


def test_parse_manifest_routes_pre_commit_config_to_its_own_parser(tmp_path: Path):
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(
        "repos:\n"
        "-   repo: https://github.com/pre-commit/mirrors-mypy\n"
        "    hooks:\n"
        "    -   id: mypy\n"
        "        additional_dependencies: ['pytest']\n"
    )
    names = {dep.name for dep in parse_manifest(config)}
    assert names == {"pytest"}


def test_parse_pre_commit_config_flow_style_split_across_lines(tmp_path: Path):
    # Real-world find: pre-commit (https://pre-commit.com/) hooks can
    # install extra packages into their own isolated environment via
    # `additional_dependencies` -- most commonly type stubs/typed-library
    # pins for a `mypy` hook. Before this fix, `.pre-commit-config.yaml`
    # had no PARSERS/find_manifests entry at all, so a hallucinated name
    # planted here was silently never scanned, both via directory
    # discovery and when the file was pointed at directly ("don't know
    # how to parse this file").
    #
    # Confirmed live with real pre-commit 4.6.2: a scratch repo with this
    # exact shape (a mirrors-mypy hook, additional_dependencies carrying a
    # hallucinated name) made `pre-commit run` genuinely execute `pip
    # install . types-PyYAML totally-hallucinated-slopcheck-test-pkg-
    # xyz-42==1.0.0` inside the hook's own fresh virtualenv and fail with
    # pip's real "ERROR: Could not find a version that satisfies the
    # requirement ... (from versions: none)".
    #
    # This exact multi-line-flow-sequence shape (the `[` on its own line
    # after the colon, not on the same line) is axolotl's own real,
    # current `.pre-commit-config.yaml` (found on this box), not a
    # contrived fixture.
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(
        """
        repos:
        -   repo: https://github.com/pre-commit/mirrors-mypy
            rev: v1.19.1
            hooks:
            - id: mypy
              additional_dependencies:
                [
                    'types-PyYAML',
                    'totally-hallucinated-slopcheck-precommit-xyz-42==1.0.0',
                ]
        """
    )
    names = {dep.name for dep in parse_pre_commit_config(config)}
    assert names == {"types-PyYAML", "totally-hallucinated-slopcheck-precommit-xyz-42"}


def test_parse_pre_commit_config_block_style_multiple_hooks(tmp_path: Path):
    # Each hook's additional_dependencies list must be bounded
    # independently -- a block-sequence entry in one hook must not bleed
    # into a sibling hook's list, and vice versa.
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(
        """
        repos:
        - repo: https://github.com/pre-commit/mirrors-mypy
          hooks:
          - id: mypy
            additional_dependencies:
            - types-requests
            - totally-hallucinated-precommit-block-xyz-1
          - id: flake8
            additional_dependencies:
            - flake8-bugbear  # inline comment
        """
    )
    names = {dep.name for dep in parse_pre_commit_config(config)}
    assert names == {
        "types-requests",
        "totally-hallucinated-precommit-block-xyz-1",
        "flake8-bugbear",
    }


def test_parse_pre_commit_config_node_ecosystem_hook_not_misread_as_pypi(tmp_path: Path):
    # Deliberately out of scope, not a bug: a version-pinned npm-ecosystem
    # hook's additional_dependencies (e.g. pre-commit/mirrors-eslint's own
    # documented usage, `eslint@4.15.0`) must not be misread as a PyPI
    # name. `@`-joined npm version syntax doesn't match `_REQ_LINE_RE` at
    # all, the same way it's already excluded from every other
    # pip-specifier reader in this module.
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(
        """
        repos:
        - repo: https://github.com/pre-commit/mirrors-eslint
          hooks:
          - id: eslint
            additional_dependencies:
            - eslint@4.15.0
            - eslint-plugin-react@6.10.3
        """
    )
    names = {dep.name for dep in parse_pre_commit_config(config)}
    assert names == set()


def test_parse_pre_commit_config_hook_without_additional_dependencies(tmp_path: Path):
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(
        """
        repos:
        - repo: https://github.com/pre-commit/pre-commit-hooks
          hooks:
          - id: check-yaml
          - id: end-of-file-fixer
        """
    )
    assert parse_pre_commit_config(config) == []


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


def test_parse_pipfile_skips_svn_hg_bzr_vcs_sources(tmp_path: Path):
    # Real-world find: Pipenv's own VCS_LIST (pipenv/utils/constants.py in
    # Pipenv 2026.8.0) is `("git", "svn", "hg", "bzr")` -- its schema
    # (pipenv.vendor.plette.models.packages.PackageSpecfiers) accepts all
    # four as equally valid dependency-spec keys, and its own `is_vcs()`
    # helper treats them identically. Confirmed live: `pipenv lock -v`
    # against a Pipfile entry `{svn = "svn://127.0.0.1:9/repo"}` genuinely
    # dispatched to pip's own Subversion VCS backend and never queried PyPI
    # for that name -- the same non-registry-source shape as `git`, which
    # `_is_pipfile_registry_dep` already handled, but `svn`/`hg`/`bzr` did
    # not: before this fix, each of these entries was sent to PyPI and
    # reported as a plain hallucination unless the name happened to be
    # independently published there.
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        """
        [packages]
        requests = "*"
        internal-svn-lib = { svn = "svn://example.com/internal-svn-lib" }
        internal-hg-lib = { hg = "https://example.com/internal-hg-lib" }
        internal-bzr-lib = { bzr = "bzr+ssh://example.com/internal-bzr-lib" }
        """
    )
    names = {dep.name for dep in parse_pipfile(pipfile)}
    assert names == {"requests"}


def test_parse_pipfile_reads_custom_categories(tmp_path: Path):
    # Real-world find: Pipenv's own `get_package_categories()` (pipenv/
    # utils/pipfile.py, Pipenv 2026.8.0) treats every top-level Pipfile
    # table as a package category except a fixed exclusion list
    # ("build-system", "pipenv", "requires", "scripts", "source") -- an
    # arbitrary section name like `[feature-x-packages]` is a real,
    # documented Pipenv feature (`pipenv install --categories`), not a
    # typo. Confirmed live (Pipenv 2026.8.0): a Pipfile with a hallucinated
    # name sitting only in such a custom category made a real `pipenv lock`
    # genuinely try and fail to resolve it from PyPI, the identical failure
    # as a name in `[packages]`. Before this fix, `parse_pipfile` only ever
    # read the two literal section names "packages"/"dev-packages", so this
    # name was silently never checked at all.
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        """
        [[source]]
        name = "pypi"
        url = "https://pypi.org/simple"
        verify_ssl = true

        [packages]
        requests = "*"

        [feature-x-packages]
        totally-hallucinated-pipenv-category-xyz-123 = "*"

        [requires]
        python_version = "3.13"
        """
    )
    names = {dep.name for dep in parse_pipfile(pipfile)}
    assert names == {"requests", "totally-hallucinated-pipenv-category-xyz-123"}


def test_parse_pipfile_skips_non_category_sections(tmp_path: Path):
    # Mirror image of the test above: `[pipenv]`/`[scripts]` are real
    # Pipfile tables (Pipenv config, shell-script aliases) that are never
    # package categories, confirmed against Pipenv's own
    # `NON_CATEGORY_SECTIONS` constant. Neither should ever contribute a
    # "dependency" name, even though both are plain key=value TOML tables
    # structurally indistinguishable from `[packages]`.
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text(
        """
        [packages]
        requests = "*"

        [pipenv]
        allow_prereleases = true

        [scripts]
        test = "pytest"
        """
    )
    names = {dep.name for dep in parse_pipfile(pipfile)}
    assert names == {"requests"}


def test_find_manifests_discovers_pipfile(tmp_path: Path):
    (tmp_path / "Pipfile").write_text('[packages]\nrequests = "*"\n')
    found = {p.name for p in find_manifests(tmp_path)}
    assert "Pipfile" in found


def test_parse_manifest_routes_pipfile_to_pipfile_parser(tmp_path: Path):
    pipfile = tmp_path / "Pipfile"
    pipfile.write_text('[packages]\nrequests = "*"\n')
    names = {dep.name for dep in parse_manifest(pipfile)}
    assert names == {"requests"}


def test_parse_pylock_toml(tmp_path: Path):
    # Real-world find: PEP 751's `pylock.toml` (real pip 26.1+ support, `pip
    # install -r pylock.toml`) had no filename entry in `PARSERS`/
    # `find_manifests` at all, so a project locked this way was never
    # scanned. Shape confirmed live: `pip lock -r req.txt -o pylock.toml`
    # (pip 26.2.1) against a plain `requests==2.32.3` requirement produced
    # exactly this `[[packages]] name = "..." version = "..."
    # [[packages.wheels]] url = "https://files.pythonhosted.org/..."` shape.
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        """
        lock-version = "1.0"
        created-by = "pip"

        [[packages]]
        name = "requests"
        version = "2.32.3"

        [[packages.wheels]]
        name = "requests-2.32.3-py3-none-any.whl"
        url = "https://files.pythonhosted.org/packages/f9/9b/requests-2.32.3-py3-none-any.whl"

        [packages.wheels.hashes]
        sha256 = "70761cfe03c773ceb22aa2f671b4757976145175cdfca038c02654d061d6dcc6"

        [[packages]]
        name = "totally-hallucinated-pylock-test-xyz-987"
        version = "1.0.0"

        [[packages.wheels]]
        name = "totally_hallucinated_pylock_test_xyz_987-1.0.0-py3-none-any.whl"
        url = "https://files.pythonhosted.org/packages/aa/bb/totally_hallucinated_pylock_test_xyz_987-1.0.0-py3-none-any.whl"

        [packages.wheels.hashes]
        sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
        """
    )
    deps = parse_pylock_toml(lock)
    names = {dep.name for dep in deps}
    assert names == {"requests", "totally-hallucinated-pylock-test-xyz-987"}
    assert all(dep.ecosystem == "pypi" for dep in deps)
    assert all(dep.source == str(lock) for dep in deps)


def test_parse_pylock_toml_skips_vcs_and_directory_sources(tmp_path: Path):
    # PEP 751 lets a `[[packages]]` entry's actual files come from a VCS
    # checkout (`packages.vcs`) or a local directory (`packages.directory`,
    # how a locked project's own editable/source-tree install is recorded)
    # instead of a downloadable archive/sdist/wheel -- confirmed live that
    # real pip never queries PyPI for either shape (it clones/reads the local
    # tree directly), the same non-registry-source situation already handled
    # for Poetry/uv/Pipfile's own git/path table forms.
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        """
        lock-version = "1.0"
        created-by = "pip"

        [[packages]]
        name = "requests"
        version = "2.32.3"

        [[packages.wheels]]
        url = "https://files.pythonhosted.org/packages/f9/9b/requests-2.32.3-py3-none-any.whl"

        [packages.wheels.hashes]
        sha256 = "70761cfe03c773ceb22aa2f671b4757976145175cdfca038c02654d061d6dcc6"

        [[packages]]
        name = "internal-git-lib"
        [packages.vcs]
        type = "git"
        url = "https://example.com/internal-git-lib.git"

        [[packages]]
        name = "internal-editable-project"
        [packages.directory]
        path = "."
        editable = true
        """
    )
    names = {dep.name for dep in parse_pylock_toml(lock)}
    assert names == {"requests"}


def test_parse_pylock_toml_skips_path_only_archive_sdist_and_wheels(tmp_path: Path):
    # Real-world find: a `packages.archive`/`packages.sdist`/
    # `[[packages.wheels]]` entry resolves its actual file via *either* a
    # `url` key *or* a `path` key (PEP 751's own schema) -- `path` is a
    # local filesystem reference, not a downloadable one. Confirmed live
    # (uv 0.12.19): a dependency pinned via `[tool.uv.sources]`'s file-path
    # form (`{ path = "../dist/<name>-0.1.0-py3-none-any.whl" }`, naming one
    # exact local wheel rather than a source directory) made `uv export
    # --format pylock.toml` write `archive = { path = "...", hashes = {...} }`
    # with no `url` anywhere in the entry -- uv never queried PyPI for that
    # name. Before this fix, only `vcs`/`directory` opted a `[[packages]]`
    # entry out of the registry check, so this url-less archive/sdist/wheels
    # shape was still checked against PyPI and flagged `not_found` for a
    # genuinely local-only, never-published package.
    lock = tmp_path / "pylock.toml"
    lock.write_text(
        """
        lock-version = "1.0"
        created-by = "uv"

        [[packages]]
        name = "requests"
        version = "2.32.3"

        [[packages.wheels]]
        url = "https://files.pythonhosted.org/packages/f9/9b/requests-2.32.3-py3-none-any.whl"

        [packages.wheels.hashes]
        sha256 = "70761cfe03c773ceb22aa2f671b4757976145175cdfca038c02654d061d6dcc6"

        [[packages]]
        name = "local-archive-only-pkg"
        version = "0.1.0"
        archive = { path = "../dist/local-archive-only-pkg-0.1.0.tar.gz", hashes = { sha256 = "0" } }

        [[packages]]
        name = "local-sdist-only-pkg"
        version = "0.1.0"
        sdist = { path = "../dist/local-sdist-only-pkg-0.1.0.tar.gz", hashes = { sha256 = "0" } }

        [[packages]]
        name = "local-wheel-only-pkg"
        version = "0.1.0"

        [[packages.wheels]]
        path = "../dist/local_wheel_only_pkg-0.1.0-py3-none-any.whl"

        [packages.wheels.hashes]
        sha256 = "0"
        """
    )
    names = {dep.name for dep in parse_pylock_toml(lock)}
    assert names == {"requests"}


def test_find_manifests_discovers_pylock_toml_and_named_variant(tmp_path: Path):
    (tmp_path / "pylock.toml").write_text('[[packages]]\nname = "requests"\n')
    named_dir = tmp_path / "sub"
    named_dir.mkdir()
    (named_dir / "pylock.dev.toml").write_text('[[packages]]\nname = "flask"\n')
    found = {p.name for p in find_manifests(tmp_path)}
    assert "pylock.toml" in found
    assert "pylock.dev.toml" in found


def test_parse_manifest_routes_pylock_toml_to_pylock_parser(tmp_path: Path):
    lock = tmp_path / "pylock.toml"
    lock.write_text('[[packages]]\nname = "requests"\n')
    names = {dep.name for dep in parse_manifest(lock)}
    assert names == {"requests"}


def test_parse_manifest_routes_named_pylock_variant_to_pylock_parser(tmp_path: Path):
    lock = tmp_path / "pylock.ci.toml"
    lock.write_text('[[packages]]\nname = "requests"\n')
    names = {dep.name for dep in parse_manifest(lock)}
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


def test_parse_setup_cfg_reads_setup_requires(tmp_path: Path):
    # Real-world find: `[options] setup_requires` (packages setuptools
    # installs into the build environment before running setup.py at all,
    # the setup.cfg analog of PEP 518's `[build-system] requires`) had no
    # reader here at all — confirmed live: setuptools 84.0.0 (this
    # project's own pinned minimum) still parses this field
    # (`ConfigOptionsHandler.parsers['setup_requires']`), and pyscaffold's
    # own real, current setup.cfg uses it (`setup_requires =
    # pyscaffold>=3.2a0,<3.3a0`). Before this fix, a setup.cfg with a real
    # `install_requires` entry and a hallucinated name planted only in
    # `setup_requires` reported "1 dependency checked, all clean" —
    # silently saying nothing about the fabricated build-time dependency.
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[options]\n"
        "install_requires =\n"
        "    requests\n"
        "setup_requires =\n"
        "    setuptools_scm\n"
        "    definitely-not-a-real-hallucinated-pkg-xyz123\n"
    )
    names = {dep.name for dep in parse_setup_cfg(cfg)}
    assert names == {"requests", "setuptools_scm", "definitely-not-a-real-hallucinated-pkg-xyz123"}


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


def test_parse_setup_cfg_single_line_semicolon_list(tmp_path: Path):
    # Real-world find: setuptools' own list parser (`ConfigHandler._parse_list`,
    # shared by `install_requires`/`setup_requires`/`options.extras_require`)
    # splits a value with no newline in it on ";" rather than "\n" — confirmed
    # live against setuptools 84.0.0 (this project's own pinned minimum):
    # `setuptools.config.setupcfg.read_configuration` on
    # `install_requires = requests;totally-hallucinated-package-xyz-123`
    # returns `['requests', 'totally-hallucinated-package-xyz-123']`, two
    # real requirements. Before this fix, `_setup_cfg_list_deps` only ever
    # split on newlines, so a single-line semicolon list like this one was
    # treated as one whole line and matched by `_REQ_LINE_RE` as a single
    # requirement named "requests" — everything after the first ";"
    # (including a second, hallucinated package name) was silently
    # swallowed as if it were an environment marker and never checked.
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[options]\n"
        "install_requires = requests;totally-hallucinated-package-xyz-123\n"
        "setup_requires = setuptools_scm;another-hallucinated-pkg-xyz456\n"
        "\n"
        "[options.extras_require]\n"
        "test = pytest;yet-another-hallucinated-pkg-xyz789\n"
    )
    names = {dep.name for dep in parse_setup_cfg(cfg)}
    assert names == {
        "requests",
        "totally-hallucinated-package-xyz-123",
        "setuptools_scm",
        "another-hallucinated-pkg-xyz456",
        "pytest",
        "yet-another-hallucinated-pkg-xyz789",
    }


def test_parse_setup_cfg_tab_before_comment_hash_not_stripped(tmp_path: Path):
    # Real-world find: setuptools' own requirement-string preprocessing
    # (`setuptools._reqs.parse_strings`, which maps `jaraco.text.drop_comment`
    # over each already-stripped line before ever constructing a
    # `packaging.requirements.Requirement` from it -- confirmed by reading
    # jaraco.text 4.0.0's vendored source, the exact copy setuptools 84.0.0
    # vendors) does NOT use pip's own "any whitespace before #" comment rule
    # (`_strip_inline_comment`, which correctly mirrors pip's real
    # `req_file.COMMENT_RE` for requirements.txt-format files everywhere else
    # in this module). `drop_comment`'s own implementation is just
    # `line.partition(' #')[0]`: a single literal space immediately before
    # `#`, not any whitespace. A trailing comment preceded by a tab (an
    # ordinary tab-aligned-comment editor habit) doesn't match, so the
    # "comment" -- `#` and all -- stays glued onto the requirement string in
    # real setuptools.
    #
    # Confirmed live (setuptools 84.0.0, real `python -m build --sdist`
    # against exactly this setup.cfg): the build Fataled immediately with
    # `packaging.requirements.InvalidRequirement: Expected semicolon (after
    # name with no version specifier) or end`, pointing at the
    # tab-separated comment, before resolving a single dependency -- real or
    # hallucinated. Before this fix, `_setup_cfg_list_deps` reused
    # `_strip_inline_comment` (pip's requirements.txt rule, where a tab is
    # just more whitespace) here, silently stripping the tab-preceded
    # comment and reporting "requests" as an ordinary, checkable dependency
    # -- implying this setup.cfg installs cleanly, when a real
    # `pip install .`/`python -m build` genuinely crashes on it before any
    # dependency, including the hallucinated one on the next, syntactically
    # fine line, is ever resolved.
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[options]\n"
        "install_requires =\n"
        "\trequests\t# http client\n"
        "\ttotally-hallucinated-package-xyz-456\n"
    )
    names = {dep.name for dep in parse_setup_cfg(cfg)}
    assert names == {"totally-hallucinated-package-xyz-456"}
    assert "requests" not in names


def test_parse_setup_cfg_install_requires_file_directive(tmp_path: Path):
    # Real-world find: setuptools' actual `install_requires`/
    # `[options.extras_require]` parser (`_parse_requirements_list`, confirmed
    # live by reading setuptools 84.0.0's `setupcfg.py`) treats a value
    # starting with the literal "file:" as a comma-separated list of paths
    # *relative to the directory containing setup.cfg*, reads them, and joins
    # their contents before splitting into individual requirements — it's not
    # a literal string to check against PyPI. Confirmed against a real,
    # currently-maintained PyPI package: `CleanCut/green`'s shipped setup.cfg
    # has `install_requires = file:requirements.txt` and
    # `[options.extras_require] dev = file:requirements-dev.txt`, both
    # resolving (confirmed live with setuptools 84.0.0's own
    # `read_configuration`) to real requirement lists. Before this fix,
    # `_setup_cfg_list_deps` received the literal string
    # `"file:requirements.txt"` unchanged; `_REQ_LINE_RE` doesn't match it
    # (":" isn't a valid name/version-specifier character), so every
    # dependency in the referenced file silently vanished — the same "0
    # dependencies checked, all clean" false-all-clear shape as the
    # Pipfile/`[build-system] requires` gaps already fixed here. Also covers
    # a comma-separated multi-file value (setuptools' own documented form,
    # `file: a.txt, b.txt`) and confirms `[options] setup_requires` is
    # deliberately unaffected: its parser entry
    # (`self._parse_list_semicolon`) never expands `file:` at all, so a
    # literal `file:...` value there stays inert rather than resolving.
    (tmp_path / "requirements.txt").write_text("requests\ntotally-hallucinated-package-xyz-987\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "extra-dev.txt").write_text("another-hallucinated-pkg-xyz654\n")
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[options]\n"
        "install_requires = file:requirements.txt\n"
        "setup_requires = file:requirements.txt\n"
        "\n"
        "[options.extras_require]\n"
        "dev = file: requirements-dev.txt, extra-dev.txt\n"
    )
    deps = parse_setup_cfg(cfg)
    names = {dep.name for dep in deps}
    assert names == {
        "requests",
        "totally-hallucinated-package-xyz-987",
        "pytest",
        "another-hallucinated-pkg-xyz654",
    }
    sources = {dep.name: dep.source for dep in deps}
    assert sources["requests"] == str(tmp_path / "requirements.txt")
    assert sources["pytest"] == str(tmp_path / "requirements-dev.txt")
    assert sources["another-hallucinated-pkg-xyz654"] == str(tmp_path / "extra-dev.txt")


def test_parse_setup_cfg_file_directive_does_not_follow_pip_directives(tmp_path: Path):
    # Real-world find: setuptools' own `file:` directive (`_parse_file` +
    # `_parse_requirements_list` in setupcfg.py, confirmed live against
    # setuptools 84.0.0) is NOT a real pip requirements-file reader. It reads
    # the referenced file's raw text and splits it on newline/";" exactly
    # like a plain (non-`file:`) setup.cfg list value -- there's no `-r`
    # recursion step, and a `-i`/`-e`/`-c` line isn't skipped as a directive,
    # it's kept as a literal string and handed to
    # `packaging.requirements.Requirement()` at build time. Live-verified: a
    # real `pip install .`/`python -m build` against a setup.cfg whose
    # `file:`-referenced requirements file contains "-r other.txt" Fatals
    # immediately with `InvalidRequirement: Expected package name at the
    # start of dependency specifier`, before any dependency -- including an
    # innocent sibling requirement on the next line, or anything in the
    # referenced "-r" target -- is ever resolved.
    #
    # Before this fix, `_setup_cfg_requirements_value` read the `file:`
    # target with `parse_requirements_txt` (the real pip requirements-file
    # parser), which genuinely follows a "-r other.txt" line into the
    # referenced file and reports whatever's in there as an ordinary
    # dependency of the project -- actively misleading, since the real tool
    # never gets far enough to resolve (or even attempt to resolve) that name
    # at all; the real, actionable problem (the malformed "-r" line itself,
    # which breaks the build outright) was never surfaced.
    (tmp_path / "base-reqs.txt").write_text("-r inner-reqs.txt\nflask\n")
    (tmp_path / "inner-reqs.txt").write_text("totally-hallucinated-filedirective-xyz-999\n")
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[options]\ninstall_requires = file:base-reqs.txt\n")
    deps = parse_setup_cfg(cfg)
    names = {dep.name for dep in deps}
    assert names == {"flask"}
    assert "totally-hallucinated-filedirective-xyz-999" not in names


def test_parse_setup_cfg_skips_self_referential_extras(tmp_path: Path):
    # Real-world find: `parse_pyproject_toml` already skips a PEP 621
    # project's own self-referential extra (an [project.optional-
    # dependencies] entry naming the *current* project with a different
    # combination of its own extras, e.g. `all = ["your-project[gui,cli]"]`,
    # so an umbrella extra doesn't need a hand-maintained copy of every
    # other extra's list) via `_self_referential_name`, but `parse_setup_cfg`
    # had no equivalent skip-set at all. The same pattern is exactly as
    # legal for setup.cfg's own [options.extras_require]: it's a property of
    # how pip resolves a self-named Requires-Dist against the package
    # already being installed, not something specific to PEP 621 syntax.
    # Confirmed live (setuptools 84.0.0, real pip 25.x): a from-scratch
    # setup.cfg-only project ([metadata] name = totally-hallucinated-
    # selfref-test-xyz-123, [options.extras_require] all = totally-
    # hallucinated-selfref-test-xyz-123[gui], gui = pillow, no PEP 621
    # [project] table at all) had `pip install --dry-run -v ".[all]"`
    # resolve the "all"/"gui" extras and install pillow without ever issuing
    # a single request for "totally-hallucinated-selfref-test-xyz-123"
    # itself. Before this fix, `parse_setup_cfg` emitted that self-reference
    # as an ordinary dependency, and a real `slopcheck` run against the
    # reproduction project above genuinely flagged it "NOT FOUND (no such
    # project on PyPI)" — a false positive for the exact not-yet-published-
    # project case this pattern exists for. The name is matched PEP
    # 503-normalized (case/separator-insensitive), like every other
    # name-equality check in this module, and `setup_requires` is
    # deliberately unaffected: it installs into an isolated build
    # environment before the package's own metadata/extras exist at all, so
    # a self-reference there wouldn't resolve locally the way it does in the
    # two fields covered here.
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[metadata]\n"
        "name = Totally-Hallucinated_Selfref.Test-XYZ-123\n"
        "\n"
        "[options]\n"
        "install_requires =\n"
        "    requests\n"
        "\n"
        "[options.extras_require]\n"
        "gui =\n"
        "    pillow\n"
        "all =\n"
        "    totally-hallucinated-selfref-test-xyz-123[gui]\n"
    )
    names = {dep.name for dep in parse_setup_cfg(cfg)}
    assert names == {"requests", "pillow"}


def test_find_manifests_discovers_setup_cfg(tmp_path: Path):
    (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires =\n    requests\n")
    found = {p.name for p in find_manifests(tmp_path)}
    assert "setup.cfg" in found


def test_parse_manifest_routes_setup_cfg_to_setup_cfg_parser(tmp_path: Path):
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[options]\ninstall_requires =\n    requests\n")
    names = {dep.name for dep in parse_manifest(cfg)}
    assert names == {"requests"}


def test_parse_environment_yml_reads_pip_section_only(tmp_path: Path):
    # Real-world find: a conda `environment.yml`/`environment.yaml` had no
    # filename entry in `PARSERS`/`find_manifests` at all, so a conda-based
    # project's PyPI dependencies -- listed under `dependencies: - pip: -
    # ...`, per conda's own documented "mixed" format -- were never scanned.
    # Shape confirmed against a real, currently-used file, CompVis/latent-
    # diffusion's actual `environment.yaml`
    # (https://github.com/CompVis/latent-diffusion/blob/main/environment.yaml).
    # Confirmed against conda's own source
    # (conda/env/installers/pip.py, `install()`): every `pip:` list entry is
    # written verbatim into a temporary `requirements.txt` and installed via
    # a real `pip install -U -r <tmpfile>` subprocess call -- so a
    # hallucinated name placed there is genuinely sent to PyPI by `conda env
    # create`, exactly like an ordinary `requirements.txt` entry, while the
    # top-level `dependencies:` list items (`python=3.8.5`, `pytorch=1.11.0`,
    # `cudatoolkit=11.3`) are conda packages resolved from conda channels, not
    # PyPI, and must NOT be checked against it (`cudatoolkit`/`pytorch`
    # pinned this way aren't real PyPI releases at all, or aren't the same
    # package if they happen to exist there).
    env_file = tmp_path / "environment.yaml"
    env_file.write_text(
        "\n".join(
            [
                "name: ldm",
                "channels:",
                "  - pytorch",
                "  - defaults",
                "dependencies:",
                "  - python=3.8.5",
                "  - pip=20.3",
                "  - cudatoolkit=11.3",
                "  - pytorch=1.11.0",
                "  - numpy=1.19.2",
                "  - pip:",
                "    - diffusers",
                "    - opencv-python==4.1.2.30",
                "    - test-tube>=0.7.5",
                "    - totally-hallucinated-condapip-xyz-123",
                "    - -e git+https://github.com/CompVis/taming-transformers.git@master#egg=taming-transformers",
                "    - -e .",
            ]
        )
    )
    deps = parse_environment_yml(env_file)
    names = {dep.name for dep in deps}
    assert names == {
        "diffusers",
        "opencv-python",
        "test-tube",
        "totally-hallucinated-condapip-xyz-123",
    }
    assert all(dep.ecosystem == "pypi" for dep in deps)
    assert all(dep.source == str(env_file) for dep in deps)


def test_parse_environment_yml_reads_pip_section_with_commented_headers(tmp_path: Path):
    # Regression test for a real-world find, the `environment.yml` sibling
    # of `test_pnpm_workspace_root_finds_ancestor_with_commented_packages_
    # header` above: `_environment_yml_pip_requirement_lines` had zero
    # handling for a trailing `#` comment on the `dependencies:`/`pip:`
    # header lines themselves (a real, common annotation, same as any other
    # YAML key). Confirmed live (no mocking, direct function test): the
    # comment made `top_match.group(2)`/the `"pip:"` equality check read as
    # non-empty/non-matching, so the entire nested `pip:` block was wrongly
    # treated as absent -- a hallucinated package name placed there was
    # silently never checked at all, the exact false-all-clear shape this
    # function's own docstring already describes fixing once before.
    env_file = tmp_path / "environment.yml"
    env_file.write_text(
        "\n".join(
            [
                "dependencies:  # conda + pip mix",
                "  - python=3.8.5",
                "  - pip:  # pypi-only extras",
                "    - totally-hallucinated-commentedheader-xyz-456",
            ]
        )
    )
    deps = parse_environment_yml(env_file)
    names = {dep.name for dep in deps}
    assert names == {"totally-hallucinated-commentedheader-xyz-456"}


def test_parse_environment_yml_recurses_into_nested_r_file(tmp_path: Path):
    # Real-world find: conda's own `conda/env/installers/pip.py` `install()`
    # writes the `pip:` list into a temp requirements file inside
    # `get_pip_workdir(args.file)` -- confirmed by reading that function
    # directly (`os.path.dirname(os.path.abspath(<environment.yml path>))`,
    # i.e. this file's own directory, not a throwaway tmpdir) -- and runs
    # `pip install -U -r <tmpfile>` with that same directory as `cwd`. Real
    # pip's own requirements-file parser then recurses into a `-r`/
    # `--requirement` target from there exactly like it would for a
    # standalone requirements.txt, resolved relative to that directory.
    # Live-verified end-to-end with a real Miniforge/conda 26.7.2 install: an
    # environment.yml whose `pip:` list was just `- -r requirements-dev.txt`,
    # with a sibling `requirements-dev.txt` naming a hallucinated package,
    # made `conda env create` genuinely try (and fail) to `pip install` that
    # name. Before this fix, `parse_environment_yml` matched each `pip:`
    # line against `_REQ_LINE_RE` directly with only an `-e `/`--`/`://`
    # skip inlined by hand -- `-r requirements-dev.txt` matches neither that
    # skip nor `_REQ_LINE_RE` (no leading name character), so it was
    # silently dropped and the nested file's dependencies, hallucinated or
    # not, were never checked at all: a real "0 dependencies checked, all
    # clean" false-all-clear for a conda project splitting its pip
    # dependencies across files the same ordinary way a standalone
    # requirements.txt project already can (already handled there, just
    # never wired into this sibling parser).
    (tmp_path / "requirements-dev.txt").write_text(
        "requests==2.31.0\ntotally-hallucinated-conda-nested-req-xyz-777==1.0.0\n"
    )
    env_file = tmp_path / "environment.yml"
    env_file.write_text(
        "\n".join(
            [
                "name: nestedtest",
                "dependencies:",
                "  - python=3.11",
                "  - pip",
                "  - pip:",
                "    - -r requirements-dev.txt",
                "    - flask",
            ]
        )
    )
    deps = parse_environment_yml(env_file)
    names = {dep.name for dep in deps}
    assert names == {"requests", "totally-hallucinated-conda-nested-req-xyz-777", "flask"}
    by_name = {dep.name: dep for dep in deps}
    assert by_name["requests"].source == str(tmp_path / "requirements-dev.txt")
    assert by_name["flask"].source == str(env_file)


def test_parse_environment_yml_joins_backslash_continuation_in_pip_section(tmp_path: Path):
    # Real-world find: conda's own `conda/env/installers/pip.py` `install()`
    # writes every `pip:` list entry into a temp requirements file as its
    # own physical line (`"\n".join(specs)`, confirmed by reading that
    # function directly -- same source already cited in
    # `test_parse_environment_yml_reads_pip_section_only` above) and runs a
    # real `pip install -U -r <tmpfile>` subprocess against it. That temp
    # file is just as subject to pip's own `req_file.join_lines`
    # backslash-continuation joining as any other requirements.txt --
    # confirmed live (real pip 25.1.1, `pip install --dry-run -r` against a
    # temp file built the exact way conda's `install()` builds one, from a
    # two-entry `pip:` list reading "totally-hallucinated-xyz-987 \\" then
    # "==1.2.3"): pip genuinely joined them into one requirement and failed
    # resolving it ("ERROR: Could not find a version that satisfies the
    # requirement totally-hallucinated-xyz-987==1.2.3 (from versions:
    # none)") -- the identical failure shape as the already-fixed
    # standalone-requirements.txt case
    # (test_parse_requirements_txt_joins_backslash_continuation above).
    #
    # Before this fix, `parse_environment_yml` passed the extracted `pip:`
    # lines straight to `_parse_requirement_lines` with no
    # continuation-joining at all (unlike `_parse_requirements_txt`, which
    # already called `_join_backslash_continuations`): the first line's
    # lone trailing "\\" doesn't match `_REQ_LINE_RE`, and the continuation
    # line ("==1.2.3") has no leading name character either, so neither
    # half ever became a `Dependency` -- the hallucinated name was silently
    # never checked even though a real `conda env create` genuinely tries
    # to resolve it. The same fix already shipped for requirements.txt in
    # v0.1.54 was never ported to this independent extraction path.
    env_file = tmp_path / "environment.yml"
    env_file.write_text(
        "\n".join(
            [
                "name: backslashtest",
                "dependencies:",
                "  - python=3.11",
                "  - pip",
                "  - pip:",
                "    - totally-hallucinated-xyz-987 \\",
                "    - ==1.2.3",
                "    - requests==2.32.3",
            ]
        )
    )
    deps = parse_environment_yml(env_file)
    names = {dep.name for dep in deps}
    assert names == {"totally-hallucinated-xyz-987", "requests"}


def test_environment_yml_files_touched_joins_backslash_continuation(tmp_path: Path):
    # Sibling regression for `environment_yml_files_touched`, which shares
    # `_environment_yml_pip_requirement_lines`'s raw-line extraction with
    # `parse_environment_yml` above and had the identical pre-fix gap: a
    # `-r`/`--requirement` directive split across a backslash continuation
    # is just as real as a dependency spec split the same way, so it must
    # be joined before `_REQ_FILE_RE` ever sees it.
    (tmp_path / "base.txt").write_text("--extra-index-url https://pypi.internal.example/simple\n")
    env_file = tmp_path / "environment.yml"
    env_file.write_text(
        "\n".join(
            [
                "name: backslashtouchedtest",
                "dependencies:",
                "  - python=3.11",
                "  - pip",
                "  - pip:",
                "    - -r \\",
                "    - base.txt",
            ]
        )
    )
    touched = environment_yml_files_touched(env_file)
    assert tmp_path / "base.txt" in touched


def test_find_manifests_discovers_environment_yml_and_yaml(tmp_path: Path):
    (tmp_path / "environment.yml").write_text("dependencies:\n  - pip:\n    - requests\n")
    named_dir = tmp_path / "sub"
    named_dir.mkdir()
    (named_dir / "environment.yaml").write_text("dependencies:\n  - pip:\n    - flask\n")
    found = {p.name for p in find_manifests(tmp_path)}
    assert "environment.yml" in found
    assert "environment.yaml" in found


def test_parse_manifest_routes_environment_yml_to_environment_yml_parser(tmp_path: Path):
    env_file = tmp_path / "environment.yml"
    env_file.write_text("dependencies:\n  - pip:\n    - requests\n")
    names = {dep.name for dep in parse_manifest(env_file)}
    assert names == {"requests"}
