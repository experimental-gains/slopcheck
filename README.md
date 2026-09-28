# slopcheck

Catch hallucinated dependencies before you `pip install` or `npm install`
them.

LLM coding assistants occasionally invent package names that sound
plausible but don't exist — a well-documented failure mode sometimes
called "package hallucination." That's more than a typo: researchers
have shown attackers registering those exact hallucinated names ahead
of time ("slopsquatting"), so the first person to blindly install the
AI's suggestion gets whatever the attacker put there.

slopcheck reads your dependency manifests, checks every package name
against the real registry (PyPI or npm), and flags:

- **not found** — the name doesn't exist in the registry at all. If
  nothing else installed it before, this is worth a hard look.
- **recent** — the name exists but was first published in the last 30
  days. Not necessarily bad, but a same-day match between "the exact
  name my AI assistant suggested" and "a package that didn't exist
  last month" is worth a second look.
- **private** — not found on the public registry, but a private/extra
  index is configured for this project, so it may well exist there.
  slopcheck can't reach an arbitrary private registry to confirm it,
  so it reports this as unverified rather than as a hallucination.
  Doesn't fail CI under the default `--fail-on not_found` gate.
- **error** — the registry lookup itself couldn't complete (network
  failure, rate limit, registry outage), so this dependency was never
  actually verified either way. Doesn't fail CI under the default
  `--fail-on not_found` gate or `--fail-on recent` — pass `--fail-on
  error` if an unverifiable dependency should block the build rather
  than pass silently (the `error` choice itself is fixed in v0.1.46;
  before that, no `--fail-on` setting could ever fail the build on an
  errored lookup, an unintentional silent fail-open — an internal
  severity ranking placed "error" as though a stricter choice existed
  to reach it, but `--fail-on`'s own `choices=[...]` never listed one).

## If you hit "Could not find a version that satisfies the requirement" or npm's "404 Not Found"

Those are pip's and npm's own errors for exactly this situation — a
package name (typed by hand, or suggested by an AI assistant) doesn't
exist on the registry at all. Verified against a real nonexistent
package name:

```
ERROR: Could not find a version that satisfies the requirement this-package-definitely-does-not-exist-slopcheck-repro-xyz (from versions: none)
ERROR: No matching distribution found for this-package-definitely-does-not-exist-slopcheck-repro-xyz
```

```
npm ERR! code E404
npm ERR! 404 Not Found - GET https://registry.npmjs.org/this-package-definitely-does-not-exist-slopcheck-repro-xyz - Not found
```

Both only fire on a name that doesn't resolve at all. They say nothing
about a name that *does* install successfully because someone —
possibly an attacker — registered it after an AI assistant started
suggesting it. `slopcheck` checks every dependency already sitting in
your manifest, including the ones that installed without complaint,
for that gap.

A small experiment measuring where this actually happens — 88
LLM-generated dependency names checked against the real registries —
is written up in [Finding #2 of the agent-bootstrap-log](https://github.com/experimental-gains/agent-bootstrap-log#finding-2-where-llm-package-hallucination-actually-clusters):
misses clustered entirely in fast-moving/niche domains (WebGPU, ZK
rollups, homomorphic encryption, WASM tooling) and never showed up in
mainstream ones.

**Note:** a few other tools solve this same problem — notably
[0xToxSec/slopcheck](https://github.com/0xToxSec/slopcheck) (PyPI,
more ecosystems and features) and
[mattschaller/slopcheck](https://github.com/mattschaller/slopcheck)
(npm, actively maintained). Same name, independently, three times —
see [Finding #3](https://github.com/experimental-gains/agent-bootstrap-log#finding-3-slopcheck-was-already-taken--three-times)
for why. This repo is kept as-is and working, but isn't seeing active
feature development given that.

## Install

Not yet published to PyPI/npm — install straight from GitHub. Pin to
the [latest tagged release](https://github.com/experimental-gains/slopcheck/releases)
for a stable version rather than floating HEAD:

```bash
pip install git+https://github.com/experimental-gains/slopcheck.git@v0.1.47
```

## Usage

```bash
# scan the current directory (and every subdirectory) for
# requirements.txt / pyproject.toml / package.json / Pipfile / setup.cfg
slopcheck

# scan specific files
slopcheck requirements.txt package.json

# machine-readable output, e.g. for CI
slopcheck --json

# only fail CI on outright hallucinations, not merely-new packages
slopcheck --fail-on not_found
```

Exit code is non-zero when something is flagged, so it drops straight
into CI:

```yaml
- name: Check for hallucinated dependencies
  run: |
    pip install git+https://github.com/experimental-gains/slopcheck.git@v0.1.47
    slopcheck
```

## Use with pre-commit

```yaml
repos:
  - repo: https://github.com/experimental-gains/slopcheck
    rev: v0.1.47
    hooks:
      - id: slopcheck
```

Runs on any commit that touches `requirements.txt`, `pyproject.toml`,
`package.json`, `Pipfile`, or `setup.cfg`. `pre-commit` installs it into
an isolated Python environment the first time (needs Python 3.10+, no
other setup).

## Use as a Claude Code / Copilot CLI plugin

slopcheck also ships as a skill in the
[`supplychain-guard`](https://github.com/experimental-gains/claude-plugins)
plugin, so an agent checks a new Python/npm dependency name
before installing it, not just at commit time:

```
claude plugin marketplace add experimental-gains/claude-plugins
claude plugin install supplychain-guard@experimental-gains-plugins
```

Works the same way with GitHub Copilot CLI (`copilot plugin marketplace add
experimental-gains/claude-plugins`, same install command with `copilot`).

## What it checks

| File | Ecosystem |
|---|---|
| `requirements.txt` | PyPI |
| `pyproject.toml` (PEP 621 or Poetry) | PyPI |
| `Pipfile` (Pipenv) | PyPI |
| `setup.cfg` (setuptools) | PyPI |
| `package.json` | npm |

## What it isn't

- Not a malware scanner — it doesn't inspect package contents, only
  whether the name is real and how old it is.
- Not a typosquat detector — it doesn't compute edit-distance against
  popular package names, and given the note above, no plans to add
  this (0xToxSec's version already covers similar ground).
- A "recent" flag is a prompt to look closer, not proof of anything.
  Plenty of brand-new packages are legitimate.

## Same package named twice, across files or sections

When the same package name shows up more than once (e.g. `dependencies`
and `devDependencies` in one `package.json`, or across two separate
manifests), slopcheck checks it once and reports one result — but "the
same name" is decided per-ecosystem, not with a blanket lowercase
comparison. PyPI names are case- *and* separator-insensitive (PEP
503: `Foo-Bar`, `foo_bar`, and `foo.bar` all name the same
distribution), so those are treated as one entry. npm names are
case-*sensitive* on the real registry (confirmed live:
`registry.npmjs.org/lodash` → 200, `registry.npmjs.org/Lodash` → 404)
— so an npm dependency listed with two different casings is two
different (real-or-not) packages, checked and reported separately
(fixed in v0.1.25 — earlier versions lowercased every name before
comparing, which silently dropped a miscased duplicate — e.g. correct
`"axios"` in `dependencies` alongside a typo'd `"Axios"` in
`devDependencies` — from the scan entirely, with no result shown for it
at all).

## Monorepo / workspace dependencies

`npm`/`pnpm`/`Yarn` workspace protocols (`workspace:`, `file:`, `link:`,
`portal:`) and git-based specs (`git:`, `git+...`, `github:user/repo`)
point somewhere other than the public registry, so slopcheck skips them
rather than flagging every internal package name in a Turborepo/Nx/Lerna
monorepo as "not found" (fixed in v0.1.1 — v0.1.0 falsely flagged these).

A dependency version can also point at a git host with no recognized
prefix at all — a bare `"user/repo"` shorthand (npm defaults this to
GitHub), or the `gitlab:`/`bitbucket:` prefixed equivalents — which
npm resolves via `git ls-remote` against that host, never the public
registry, confirmed live (npm 9.2.0) against real GitHub, GitLab, and
Bitbucket shorthand specs regardless of whether the named repo exists.
Neither the un-prefixed form nor the `gitlab:`/`bitbucket:` prefixes
were recognized before (only `github:user/repo` was), so a dependency
written either way had its key checked against the public npm registry
and flagged "not found" — a false positive on a real, common
dependency shape (fixed in v0.1.40). Yarn Berry's own `patch:<name>@
<descriptor>#<path>` protocol (applying a local patch on top of an
otherwise normal dependency — real, current usage: babel/babel's own
`package.json` patches `@rollup/plugin-commonjs` and
`rollup-plugin-dts` this way) is deliberately *not* treated the same
way even though a real one always contains a `/` (the patch file
path): the wrapped descriptor is still a real registry reference
(`npm%3A29.0.2` is `npm:29.0.2`, URL-encoded), and Yarn genuinely
queries the registry for the named key before applying the patch —
confirmed live (Yarn Berry 4.5.0) with a real 404 from
`registry.yarnpkg.com` for a fake name inside a `patch:` spec.

`npm:` aliases (`"my-name": "npm:real-package@1.2.3"`, used to depend on
a package under a local rename or alongside another version of itself)
*are* checked, but against the real target package name rather than the
local alias key — the alias key is never expected to exist in the
registry under its own name (fixed in v0.1.2 — earlier versions checked
the alias key itself and flagged legitimate aliases as "not found").

npm's `overrides` field and Yarn's `resolutions` field (both used to
force a specific version of a package anywhere in the dependency tree,
not just top-level deps) are checked too, including nested `overrides`
entries and `resolutions` patterns that target a scoped package deep in
the tree (e.g. `"webpack/**/@babel/core"`) (added in v0.1.6 — earlier
versions didn't read either field at all, so a hallucinated name placed
there was never checked). An `overrides` key can also carry a
`@version` suffix to scope the override to one resolved version of the
package (e.g. `"kerberos@2.1.1": { "node-addon-api": "7.1.0" }`) — the
suffix is stripped before checking, so this resolves to `kerberos`, not
the literal string `kerberos@2.1.1` (fixed in v0.1.7 — earlier versions
checked the version-and-all string and always flagged it "not found").
A `resolutions` pattern's own key can carry the same kind of suffix
directly on the target package, with no `/` path at all (e.g.
`"lru-cache@^10.0.1"`, or `"@types/mdx@npm:^2.0.0"` for a scoped
package) — real syntax from jest's `package.json`, and stripped the
same way (fixed in v0.1.8 — earlier versions checked the pattern's
range/protocol suffix as part of the package name and always flagged
it "not found").

pnpm has its own `overrides` field too, nested under a top-level
`"pnpm"` key (`"pnpm": { "overrides": { ... } }`) instead of npm's
root-level `overrides` — a real, current convention (e.g. Prisma's
`package.json`) that a manifest can use in addition to, not instead
of, the root-level field. Now read the same way as npm's `overrides`,
including the same `@version`-scoped-key suffix stripping (fixed in
v0.1.14 — earlier versions only read the root-level `overrides` key,
so every package named under `pnpm.overrides` was silently never
checked).

A directory scan (`slopcheck` with no arguments, or `slopcheck <dir>`)
now recurses into subdirectories to pick up every workspace member's
own manifest, not just the one at the scanned directory's top level
(fixed in v0.1.15). Real monorepos commonly declare their actual
runtime dependencies several directories down rather than at the root
— `vitejs/vite`'s own repo is a real example: the root `package.json`
has zero runtime `dependencies` at all, while the published package's
real ones (`rolldown`, `lightningcss`, etc.) live in
`packages/vite/package.json`. Earlier versions only checked the exact
directory passed in, so running `slopcheck` at a monorepo's root
silently checked almost nothing. `node_modules` (already-*installed*
packages, not declared ones) and dot-directories (`.git`, `.venv`,
etc.) are pruned from the walk, and so are the common non-dot-prefixed
virtualenv directory names `venv`, `env`, `ENV`, `venv.bak`, and
`env.bak` (fixed in v0.1.47) — Python's own `venv` module docs say
environments are "conventionally named `.venv` or `venv`", and
GitHub's official `Python.gitignore` template lists all five as
equally real. A plain `venv` (no dot) sitting inside the scanned tree
can carry an installed package's own bundled manifest the same way
`.venv` already could: confirmed live, a fresh `venv` with pandas
installed carries pandas' own `pyproject.toml`, adding 90 dependency
specs unrelated to the project actually being scanned.

A manifest file with a leading UTF-8 byte-order mark — common from
Windows-authored files, and real enough that `vitejs/vite`'s own repo
ships a BOM'd `package.json` test fixture — is now read correctly
(fixed in v0.1.15). Earlier versions either aborted the entire scan
(`package.json`/`pyproject.toml`, one bad file taking down every other
manifest's results with it) or silently dropped just the first
dependency in the file without any error at all (`requirements.txt`).

Poetry's table-form dependencies (`{ git = "..." }`, `{ path = "..." }`,
`{ url = "..." }`) point at a git remote, a local path, or an arbitrary
URL instead of PyPI — the same non-registry-source situation as the npm
protocols above, and just as common for internal/private packages in a
Poetry monorepo. These are skipped rather than checked against PyPI
(fixed in v0.1.4 — earlier versions checked the dependency name itself
and flagged every internal git/path/url dependency as "not found").
A multiple-constraints list (`foo = [{version = "1.0", python = "<3.11"},
{version = "2.0", python = ">=3.11"}]`) is still checked as long as at
least one constraint has a real registry version.

uv's own source-override mechanism, `[tool.uv.sources]`, is the same
idea with its own independent syntax (fixed in v0.1.18 — earlier
versions didn't look at this table at all). A `git`, `path`, `workspace`,
or `url` source is skipped the same way as the Poetry table form above;
an `index` source only redirects to a *different* package index rather
than opting out of index resolution, so that name is still checked.
Found via real-world testing against marimo-team/marimo's actual
`pyproject.toml`: a bare `marimo_docs` entry sourced locally via
`[tool.uv.sources] marimo_docs = { path = "./docs", editable = true }`
genuinely 404s on PyPI, so the pre-fix parser flagged a legitimate
dependency in a ~15k-star project as hallucinated.

A dependency written with PEP 508's legacy parenthesized version
specifier (e.g. `numpy (>=1.16)`, carried over from PEP 440/setup.py-style
`install_requires` strings and still accepted by `packaging`/pip today)
is now recognized in both `pyproject.toml` and `requirements.txt` (fixed
in v0.1.9 — earlier versions only matched the bare `numpy>=1.16` form, so
a dependency written the parenthesized way was silently dropped and
never checked at all). Found via oracle-diff fuzzing against
`packaging.requirements.Requirement`, the same technique already used
across this org's Go tools, applied to a Python parser for the first
time using Hypothesis instead of Go's native fuzzer.

[PEP 735](https://peps.python.org/pep-0735/) `[dependency-groups]` — a
top-level table *sibling* to `[project]`, not nested under it like
`optional-dependencies` — is now parsed too (fixed in v0.1.12; found by
testing against real pyproject.toml files from `uv`, `pytest`, `pydantic`,
and `fastapi`, all of which use it for dev/docs/test dependencies).
Earlier versions never looked at this table at all, so any hallucinated
name placed there went unchecked — for a project with no
`[project.dependencies]` at all (`uv`'s own pyproject.toml, for example),
that meant 100% of its real dependencies were silently never checked.
An `{include-group = "other"}` entry references another group instead of
naming a package and is correctly skipped rather than treated as a
dependency name; the group it points at still gets checked on its own
turn through the table.

A [self-referential extra](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#self-referential-extras)
— an entry in `[project.optional-dependencies]` or `[dependency-groups]`
that names the *current* project with a different combination of its
own extras (e.g. `all = ["your-project-name[gui, cli]"]`, so an
"all"/"everything" extra doesn't need a hand-maintained copy of every
other extra's dependencies) — is no longer checked against the registry
as if it were an external dependency (fixed in v0.1.33). Confirmed real
and current against PDM's own `pyproject.toml`, which uses exactly this
shape in three places: `[project.optional-dependencies] template =
["pdm[copier,cookiecutter]"]` / `all = ["pdm[keyring,template]"]`, and
`[dependency-groups] test = ["pdm[pytest]", ...]` — naming `pdm` itself,
not an external package. Earlier versions matched that self-reference
like any other spec and checked the project's own name against PyPI —
harmless for an already-published project like PDM, but a spurious
"not found"/"recent" flag waiting to happen for the case this pattern
exists for: a brand-new, not-yet-published project using its own
umbrella extra while still in early development. The project's own name
is matched case/separator-insensitively (PEP 503), the same rule
`[project.name]` itself is normalized under.

A `requirements.txt` line starting with `-r`/`--requirement` (pip's
nested-requirements-file directive, resolved relative to the file that
references it) is now followed for real instead of silently skipped
(fixed in v0.1.16 — real, common examples: Home Assistant core's
`requirements_test.txt` starts with `-r
requirements_test_pre_commit.txt`; cookiecutter-django splits into
`requirements/base.txt`, `local.txt`, `production.txt` chained with
`-r`). Earlier versions never looked inside the referenced file at all,
so a hallucinated name placed only there sailed through completely
unchecked. `-c`/`--constraint` lines are deliberately still skipped
rather than followed — a name that appears only in a constraints file
and nowhere else in the requirement set has no effect on what pip
actually installs. Passing a differently-named `.txt` requirements
file directly on the command line (`slopcheck requirements_test.txt`,
rather than the auto-discovered exact name `requirements.txt`) used to
crash with an unhandled `KeyError` instead of scanning it; it's now
handled the same as `requirements.txt`.

setuptools' own [dynamic metadata](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html#dynamic-metadata)
feature lets `[project].dynamic` list `dependencies`/
`optional-dependencies` and defer their actual values to
`[tool.setuptools.dynamic]`, which points at a requirements-style file
instead of listing specs inline under `[project]` — PEP 621 requires a
field listed as dynamic to be *absent* from `[project]` directly, so
this table is the only place those dependencies exist. It's now read
the same way a standalone `requirements.txt` is (fixed in v0.1.18 —
earlier versions only looked at `[project.dependencies]`, so a project
using this feature had every dependency silently skipped). Found via
real-world testing against `compas-dev/compas`'s actual
`pyproject.toml`: `dynamic = ['dependencies', 'optional-dependencies',
'version']` with `[tool.setuptools.dynamic] dependencies = { file =
"requirements.txt" }` meant the pre-fix parser reported "0 dependencies
checked" despite 5 real runtime deps and 11 dev deps.

Pipenv's `Pipfile` (TOML, distinct from the JSON `Pipfile.lock`) is now
recognized as a manifest, reading `[packages]`/`[dev-packages]` the
same way Poetry's dependency tables are read, and skipping `git`/
`path`/`file`-sourced entries the same way (fixed in v0.1.24 — earlier
versions had no filename entry for `Pipfile` at all, so a Pipenv-only
project either hit a hard "no manifest found" error, or, worse, a
Pipenv project that also keeps tool config in a dependency-free
`pyproject.toml` — a common real combination — was silently reported
as "0 dependencies checked, all clean" while its actual dependencies
in `Pipfile` went unread).

setuptools' legacy `setup.cfg` `[options] install_requires`/
`[options.extras_require]` is now recognized as a manifest too (fixed
in v0.1.26 — earlier versions had no filename entry for `setup.cfg` at
all). This one matters more than an ordinary missing-format gap:
[PEP 518](https://peps.python.org/pep-0518/) requires any project pip
can build from source to ship a `pyproject.toml` with a `[build-system]`
table, so plenty of real, currently-maintained packages that never
migrated their dependency list to `[project.dependencies]` have a
`pyproject.toml` anyway — one containing only `[build-system]` (and
maybe `[tool.*]` config), no `[project]` table at all. Confirmed live
against `RDFLib/sparqlwrapper` and `rm-hull/luma.oled`, both real,
current GitHub repos in exactly that shape: before this fix, their
`pyproject.toml` was the only manifest found, and it legitimately
parses to zero dependencies — a silent "0 dependencies checked, all
clean" false-all-clear, same failure shape as the `Pipfile` gap above,
while every dependency actually declared in `setup.cfg` went unread.

[PEP 518](https://peps.python.org/pep-0518/)'s `[build-system] requires`
— a plain list of PEP 508 requirement strings naming the real PyPI
packages pip installs into an isolated build environment *before*
running the build at all, mandatory for any project pip can build from
source and completely independent of `[project.dependencies]` — is now
read too (fixed in v0.1.34). Confirmed real and current against numpy's
own `pyproject.toml`: `[build-system] requires = ["meson-python>=0.20.0",
"Cython>=3.1.0"]`, neither name appearing anywhere under `[project]`.
Earlier versions had no reader for this table at all, so a hallucinated
name planted only there — a real place for one to end up, since an AI
assistant asked to scaffold a build backend can invent this list the
same way it invents a runtime dependency — sailed through unchecked even
though a real `pip install` from source genuinely installs it first.

`setup.cfg`'s `[options] setup_requires` — the setup.cfg analog of PEP
518's `[build-system] requires` above, naming packages setuptools
installs into the build environment before running `setup.py` at all —
is now read too (fixed in v0.1.35). Confirmed current against
setuptools 84.0.0 (this project's own pinned minimum), which still
parses this field, and against pyscaffold's own real, current
`setup.cfg` (`[options] setup_requires = pyscaffold>=3.2a0,<3.3a0`).
Earlier versions only read `install_requires`/`extras_require`, so a
hallucinated name planted only in `setup_requires` sailed through
unchecked even though a real build genuinely installs it first — the
same failure shape as the `[build-system] requires` gap above.

PDM's own legacy `[tool.pdm.dev-dependencies]` table — predates PEP 735,
structurally identical to `[dependency-groups]` (group name -> list of
PEP 508 requirement strings) but under a different, PDM-specific table
— is now read too (fixed in v0.1.37). Confirmed live against real PDM
2.29.2: `pdm lock -v` run against a `pyproject.toml` with a fake name
planted only in `[tool.pdm.dev-dependencies]` genuinely tried to
resolve it from PyPI (`CandidateNotFound: Unable to find candidates
for ...`), so a real `pdm install`/`pdm lock` installs whatever's
planted here just as much as a `[dependency-groups]` entry — this
table isn't deprecated or inert even though PDM's own `pdm add -dG`
now defaults to writing `[dependency-groups]` instead. Earlier versions
had no reader for this table at all, so a hallucinated name planted
only in a still-common, still-honored PDM dev-dependency group sailed
through unchecked.

Hatch's own two dependency-bearing tables — `[tool.hatch.env] requires`
(environment-plugin packages Hatch installs before it can even parse
the rest of an environment's config) and each
`[tool.hatch.envs.<name>] dependencies` (plain PEP 508 requirement
strings installed into that named environment) — are now read too
(fixed in v0.1.38). Neither is PEP 621 or PEP 735; both are
Hatch-specific and had no reader here at all before this fix.
Confirmed live against real Hatch 1.18.1: `hatch env create` genuinely
tried and failed to resolve a fake name from PyPI planted in
`[tool.hatch.envs.default] dependencies` ("Could not find a version
that satisfies the requirement ... (from versions: none)"), and a
separate fake name in `[tool.hatch.env] requires` failed even earlier
while syncing environment plugin requirements ("No solution found when
resolving dependencies ... was not found in the package registry").
Earlier versions had no reader for either table, so a hallucinated
name planted in one sailed through unchecked even though a real
`hatch env create` genuinely installs it.

The `[tool.uv.sources]` git/path/workspace/url skip above (the one that
keeps `marimo_docs` from being flagged) was, until v0.1.41, applied to
*every* dependency-bearing table's output at once, including
`[build-system] requires` and Hatch's own `[tool.hatch.env]`/
`[tool.hatch.envs.*]` tables — even though neither is resolved by uv,
or consults `[tool.uv.sources]` at all (fixed in v0.1.41). Confirmed
live (venv + pip 25.x): a `pyproject.toml` with `[tool.uv.sources]
totally-hallucinated-buildreq-xyz-123 = { path = "./local-pkg" }` and
`[build-system] requires = ["setuptools",
"totally-hallucinated-buildreq-xyz-123"]` made `pip install .`
genuinely try to fetch that exact name from PyPI while installing
build dependencies (pip's build-isolation step never parses
`[tool.uv.sources]` — that table means nothing outside uv itself) and
fail with "Could not find a version that satisfies the requirement
... (from versions: none)". Before this fix, slopcheck against the
same file reported "2 dependencies checked, all clean", silently
skipping the fabricated build dependency purely because its name
happened to collide with an unrelated `[tool.uv.sources]` entry.

## Private/internal registries

A dependency that isn't on the public registry but resolves fine for a
real `pip install`/`npm install` because the project configures a
private/extra index (an internal PyPI mirror via `--extra-index-url`,
`--index-url`, or pip's short `-i` alias in `requirements.txt`,
`PIP_EXTRA_INDEX_URL`/`PIP_INDEX_URL`, or `pip.conf`; an internal npm
registry scoped to an org via `.npmrc`'s `@scope:registry=...`, or a
blanket `registry=...` override) is now reported as **private** rather
than **not found** (fixed in v0.1.10 — earlier versions had no notion
of a configured private index at all and flagged every such dependency
as a hallucination, which in practice meant every company with an
internal package would get this false alarm on every private
dependency, every CI run). Confirmed live against real `pip
install`/`npm install` resolving a throwaway package from a local
index/registry that doesn't exist on the public one. `-i` support was
added separately in v0.1.13: it's pip's own registered short option
for `--index-url` (confirmed by parsing a real `requirements.txt` with
pip's actual `parse_requirements()`), but v0.1.10 only recognized the
long-form flags, so a `requirements.txt` pointing at an internal
registry with `-i <url>` — common with Artifactory/Nexus/CodeArtifact-
style internal indexes — still got every private dependency flagged as
a hallucination until this fix. An npm scope mapping only exempts
packages under that scope — an unrelated hallucinated dependency in
the same `package.json` is still
flagged and still fails CI.

pip's own per-user config file location moves when `XDG_CONFIG_HOME`
is set — confirmed live against real pip (`pip config -v list`):
`$XDG_CONFIG_HOME/pip/pip.conf` is used *instead of*
`~/.config/pip/pip.conf`, not in addition to it, matching pip's own
vendored `platformdirs` implementation exactly. Before v0.1.19, this
tool only ever checked the `~/.config/pip/pip.conf` default, so a
private index configured via a customized `XDG_CONFIG_HOME` (a common
pattern for anyone using a dotfiles manager, or any distro/desktop
environment that sets it by default) was invisible to it — the exact
same false-hallucination failure mode this section exists to prevent,
just reached through an env var this tool wasn't reading yet.

pip's own config loader also normalizes every option key it reads,
replacing underscores with dashes before storing it (pip's own
`pip._internal.configuration._normalized_keys`) — confirmed live
(`PIP_CONFIG_FILE=... pip config -v list`) that `extra_index_url = ...`
in `pip.conf` shows up as `global.extra-index-url=...`, exactly as
effective as the canonical dash spelling, and confirmed further
(`pip install --dry-run -v` against an unreachable local index) that a
bare-underscore `pip.conf` genuinely made pip look in that index.
Before v0.1.42, this tool checked `pip.conf` with `configparser`
(which lowercases option names but never touches underscores) against
only the two dash-spelled option names, so a `pip.conf` written with
the underscore spelling — real and pip-equivalent, not invalid — was
invisible to it, the same false-hallucination failure mode as every
other gap in this section, just reached through a key spelling rather
than a file location or env var.

pip's own system-wide ("global") config lookup moves the same way its
per-user one does, but via the sibling `XDG_CONFIG_DIRS` variable
instead of `XDG_CONFIG_HOME`: real pip's vendored `platformdirs`
(`Unix._site_config_dirs`) derives this location from
`$XDG_CONFIG_DIRS` (colon-separated, falling back to `/etc/xdg` when
unset or blank), appending `/pip` to each directory, and checks
`pip.conf` there *in addition to*, not instead of, the separate
`/etc/pip.conf` this tool already checked. Confirmed live against real
pip two ways: with no environment customization at all, a `pip.conf`
placed at this box's real, unmodified default location
(`/etc/xdg/pip/pip.conf`) made `pip install --dry-run -v` against an
unreachable index genuinely print "Looking in indexes:
https://pypi.org/simple, http://127.0.0.1:9/simple"; with
`XDG_CONFIG_DIRS` set to a custom path, the same `pip.conf` placed at
`$XDG_CONFIG_DIRS/pip/pip.conf` was honored instead. Before v0.1.43,
this tool only ever checked the hardcoded `/etc/pip.conf`, so a private
index configured via this real, genuinely default pip config location
(a real deployment pattern: a Linux distro or Docker base image
dropping a managed `pip.conf` at the *actual* system-wide location
pip's own docs promise) was invisible to it, the same
false-hallucination failure mode as every other gap in this section.

npm's own per-user config file location moves the same way when
`npm_config_userconfig` (or the uppercase `NPM_CONFIG_USERCONFIG`
spelling — confirmed live that npm accepts either, or any mixed case)
is set — confirmed live against real npm (`npm config get`) that the
relocated file is read *instead of* `~/.npmrc`. Before v0.1.21, this
tool only ever checked `~/.npmrc`, so a scope-to-registry mapping
configured only via a relocated user config (the npm analog of the
`XDG_CONFIG_HOME` gap above — a real pattern for dotfiles managers and
custom CI images) was invisible to it, the same false-hallucination
failure mode reached through a different env var.

npm also reads a machine-wide *global* config file, beneath the
project/user ones in precedence but still consulted for any key they
don't set — confirmed live with a real `npm install` that a scope
mapping, or a blanket `registry=` override, placed only there is
genuinely honored. That's a real deployment pattern: an org baking a
private-registry mapping into a CI runner or Docker base image at the
machine level, so every project's and every user's own `.npmrc` stays
untouched. Before v0.1.21, this tool never read that file at all, so
either form of a global-only override was invisible to it — the
blanket form is the more consequential of the two, since it means
*every* npm dependency in the project got checked against the wrong
registry, not just packages under one scope. Its location can be
relocated the same way as `userconfig`, via `npm_config_globalconfig`/
`NPM_CONFIG_GLOBALCONFIG` — confirmed live, same case-insensitive
resolution. With no override, npm derives the location from its own
install-time global prefix, which has no single portable default (it
depends on how node/npm was installed); this tool checks the two most
common real-world locations, `/etc/npmrc` (Debian/Ubuntu's packaged
npm hardcodes this, a common Docker/CI base) and `/usr/local/etc/npmrc`
(npm's own documented default example, matching an official
installer/nvm/Homebrew-on-Linux install).

npm (and, as confirmed below, Yarn Berry too) actually matches every one
of these env vars *fully* case-insensitively — any casing of
`npm_config_registry`, `npm_config_userconfig`, or
`npm_config_globalconfig` is genuinely honored by a real `npm install`,
not just the plain-lowercase and SCREAMING_CASE spellings this tool
checked for before v0.1.28. That earlier version only ever looked for
those two spellings of each var, so a mixed-case one (plausible wherever
env vars pass through case-normalizing tooling — a Windows host, where
environment variable names are inherently case-insensitive, is the most
likely real source) was silently missed, sending the scan to the wrong
config file or ignoring a blanket private-registry override entirely —
the same false-hallucination failure mode as every other gap in this
section.

Yarn Berry (v2 and later) doesn't read `.npmrc` for its own registry
config at all — it has an entirely separate YAML config file,
`.yarnrc.yml`, with a top-level `npmRegistryServer:` key for a blanket
override and a `npmScopes.<name>.npmRegistryServer:` key for a
scope-specific one, plus a `YARN_NPM_REGISTRY_SERVER` env var
equivalent of the blanket form. Before v0.1.22, this tool only ever
looked at `.npmrc`, so a project using Yarn Berry with a private
registry configured this way had every legitimately-private dependency
flagged as a plain hallucination — confirmed live with a real `yarn
install` against a throwaway local registry, for both the blanket and
scoped forms and the env var, that Yarn genuinely routes resolution
through the configured address instead of the public npm registry. Like
npm's `.npmrc`, `.yarnrc.yml` is read from the project root and merged
with a home-directory global one (`~/.yarnrc.yml`, also confirmed live);
the filename itself can be relocated via `YARN_RC_FILENAME`, mirroring
npm's `npm_config_userconfig` — including the same full case-insensitive
matching (confirmed live the same way), and the same pre-v0.1.28 gap:
only the exact `YARN_NPM_REGISTRY_SERVER`/`YARN_RC_FILENAME` spellings
were recognized until that fix.

Bun has its own config file too, `bunfig.toml`, entirely independent
of `.npmrc`/`.yarnrc.yml` (fixed in v0.1.40 — earlier versions had no
reader for it at all). `[install].registry` (a plain URL string, or a
`{ url = ..., token = ... }` table for an authenticated registry) is a
blanket override, and `[install.scopes]` maps individual `@scope`
names to their own registry, in either form. `BUN_CONFIG_REGISTRY` is
the env var equivalent of the blanket form. Confirmed live against
real Bun (1.4.2): both a blanket `[install].registry` and a scoped
`[install.scopes]` entry genuinely routed `bun install`'s resolution
at the configured address (`ConnectionRefused` against a closed local
port, not a 404 from the public npm registry), for both the
plain-string and token-table value shapes, and the same for
`BUN_CONFIG_REGISTRY`. A `bunfig.toml` is read from the project root
and merged with a home-directory global one (`~/.bunfig.toml`, also
confirmed live), mirroring npm's/Yarn Berry's own project+global
split. Bun *also* honors a project's `.npmrc` on top of its own
config (confirmed live too), so that overlap was already covered by
the existing npm detection above — this fix only adds the config Bun
alone reads. Before this fix, a Bun project with a private registry
configured only via `bunfig.toml` had every legitimately-private
scoped dependency flagged as a plain hallucination.

Poetry has a third, independent private-registry mechanism: a
`[[tool.poetry.source]]` table in `pyproject.toml` itself, consulted by
`poetry lock`/`poetry install` regardless of any pip config or env var
(usually unset entirely in a pure-Poetry environment). A source with no
`priority` key, or `priority = "primary"`/`"supplemental"`, is a
blanket override — confirmed live with a real `poetry lock` against a
throwaway unreachable source: with no priority set, Poetry disables the
default PyPI source outright; with `"supplemental"`, PyPI is tried
first and the source is tried next for anything PyPI doesn't have,
regardless of whether any dependency references it by name. `priority
= "explicit"` is scoped instead, mirroring npm's scope mapping: it's
only ever consulted by a dependency that opts in via its own `source =
"<name>"` key — confirmed live that an unreferenced explicit source is
never contacted at all, while a dependency that does reference it
resolves only against that source. Before v0.1.23, this tool had no
notion of Poetry's own source table, so a Poetry project's
private-only dependencies (blanket or explicitly-sourced) were flagged
as plain hallucinations exactly like the pre-v0.1.10 pip/npm gap above.

An explicit-source reference is also honored inside Poetry's own
"multiple constraints" list-form dependency
(https://python-poetry.org/docs/dependency-specification/#multiple-constraints-dependencies) —
Poetry's documented way to vary a dependency's spec by Python-version or
platform marker, e.g. its own docs example of a platform-specific
compiled wheel falling back to a source repository elsewhere:
`foo = [{platform = "darwin", url = "..."}, {platform = "linux", version
= "^1.0", source = "pypi"}]`. Before v0.1.29, only a single-table spec's
`source =` key was read; a list entry's own `source` was invisible,
confirmed live with a real `poetry lock` against an unreachable
explicit source — the resolver genuinely contacted it for the matching
platform variant, the same real resolution as the single-table case
above, but this tool reported the name as a plain, unscoped
hallucination candidate instead.

Pipenv's `Pipfile` has a fourth, structurally different mechanism: a
`[[source]]` array with no `priority` key at all. Confirmed live with a
real `pipenv lock` (Pipenv 2026.8.0): with no per-package `index` key, a
dependency resolves only against the *first* `[[source]]` entry in file
order — not the entry conventionally named `"pypi"` specifically, and
not every source — so a project whose first source's `url` has been
replaced with a private mirror (keeping the usual `name = "pypi"`
Pipenv itself always writes) routes every undecorated dependency there,
the same blanket-override shape as pip's `--index-url`. A dependency's
own `index = "<name>"` key scopes it to exactly that named source
instead, mirroring Poetry's `source = "<name>"` — confirmed live going
straight to the private URL, the public source never contacted for
that name. Before v0.1.30, this tool had no notion of Pipenv's source
array at all, so either form got a private-only dependency flagged as a
plain hallucination. Because `[[source]]` is mandatory boilerplate every
real `Pipfile` carries (unlike Poetry's optional source table), the
blanket case is detected by comparing the first source's `url` against
the known public PyPI URLs, not merely by whether a source table exists
at all — otherwise every ordinary, pure-public-PyPI `Pipfile` would be
misdetected as private and silently swallow real hallucinations in it.

`uv` — now one of the most common Python dependency managers — has a
fifth, independent private-registry mechanism: a `[[tool.uv.index]]`
array of named index tables in `pyproject.toml`, or the same shape in a
standalone `uv.toml` (project-level or user-level), plus
`UV_INDEX`/`UV_DEFAULT_INDEX`/`UV_INDEX_URL`/`UV_EXTRA_INDEX_URL` env
vars. Confirmed live with a real `uv lock` against an unreachable
`127.0.0.1:9/simple` index (connection-refused as the tell, same
technique used for Poetry/Pipenv above): an index with no
`explicit = true` is consulted for *every* dependency, the same
blanket-override shape as pip's `--extra-index-url`; an `explicit =
true` index is only ever contacted for a dependency whose own
`[tool.uv.sources]` entry names it via `index = "<name>"`, mirroring
Poetry's `priority = "explicit"` — confirmed live that an unreferenced
explicit index is never even contacted for an ordinary dependency.
Before v0.1.33, this tool had no notion of `uv`'s index config at all,
so a real, `uv lock`-installable private-only dependency got reported
as a plain hallucination instead of downgraded to unverified.

PDM has a sixth, independently-shaped mechanism: a `[[tool.pdm.source]]`
array in `pyproject.toml`, scoped to specific packages via glob patterns
(`include_packages`/`exclude_packages`) on the source itself rather than
a field on the dependency spec. Confirmed live with a real `pdm lock`
(PDM 2.29.2) against an unreachable `127.0.0.1:9/simple` source: a
source with no include/exclude patterns is genuinely contacted for
*every* dependency, the same blanket shape as Pipenv's source array —
and, unlike Poetry's `priority = "explicit"` or uv's `explicit = true`,
setting `include_packages` alone does *not* stop the source from also
being consulted for a name that doesn't match the pattern (verified
live: a non-matching fake name still hit the unreachable source), since
PDM's own pattern matching only grants matching names an *exclusive*
claim rather than excluding non-matching ones. Before v0.1.33, this tool
had no notion of PDM's source table at all, so a PDM project's
private-only dependencies got reported as a plain hallucination instead
of downgraded to unverified.

A `-i`/`--extra-index-url`/`--index-url` directive is now honored no
matter which requirements-format file it's written in — not just a
file literally named `requirements.txt` passed directly on the command
line (fixed in v0.1.28). Before this fix, two real, common structures
fell through: a directive in a custom-named `.txt` file scanned
directly (e.g. `requirements-prod.txt` — this tool already treats any
`.txt` file as requirements-format, the same as pip itself doesn't
care about the filename), and a directive living only in a file pulled
in via `-r`/`--requirement` (e.g. a shared `base.txt` that every
per-environment file `-r`s into, carrying the index config so it isn't
duplicated in each one — confirmed live against real pip that a
directive placed either way applies to the whole install). Both used
to get every private-only dependency flagged as a plain hallucination.

A `-i`/`.npmrc` scope mapping/`.yarnrc.yml` registry line is now
detected even in a file that starts with a UTF-8 byte-order mark (fixed
in v0.1.37) — a real, valid file shape some editors/tools write (e.g. a
Windows-authored `requirements.txt` or `.npmrc`), and one this tool
already tolerated for the *manifest* files it reads dependency names
from. Confirmed live against all three real tools that the directive is
genuinely honored despite the BOM: pip's own output showed "Looking in
indexes: ..." and real connection attempts for a `-i`-directed,
BOM-prefixed `requirements.txt`; `npm install --loglevel=verbose`
showed a real fetch attempt at the configured address for a
BOM-prefixed `.npmrc`'s scope mapping; and real Yarn Berry (4.18.1)
refused an unencrypted-registry resolution with "Unsafe http requests
must be explicitly whitelisted" for a BOM-prefixed `.yarnrc.yml` —
proof it had genuinely read the address out of the file. Before this
fix, the leftover BOM character glued onto each file's first line broke
the regex that looks for the directive there, so this tool silently
reported no private index configured and flagged a genuinely
private-only dependency as a plain hallucination instead.

pip's own venv-root `pip.conf` (its "site" config variant) is now
recognized even when `$VIRTUAL_ENV` is unset (fixed in v0.1.44). Real
pip's own source (`pip._internal.configuration.get_configuration_files`)
derives this location from `sys.prefix`, not the `VIRTUAL_ENV`
environment variable — `VIRTUAL_ENV` is only set when a venv was
activated via its `activate` script, but invoking a venv's own pip
directly by path (`./venv/bin/pip install ...`, the standard
Dockerfile/CI/Makefile pattern that never sources `activate`) leaves
`VIRTUAL_ENV` unset while `sys.prefix` inside that process is still the
venv root. Confirmed live: a venv-root `pip.conf`'s `extra-index-url`
was genuinely honored by that venv's own `pip` with `VIRTUAL_ENV`
explicitly unset. Before this fix, this tool only checked
`$VIRTUAL_ENV/pip.conf`, so a private index configured this way was
invisible to it whenever slopcheck itself was installed into and
invoked from the same project venv without activation (e.g. `pip
install -e .[dev]` covering both the project's tests and this tool) —
a real, common setup, not an edge case — flagging a genuinely
private-only dependency as a plain hallucination instead of downgrading
it to private.

A `package.json`'s plain `"workspaces"` field (npm 7+ native
workspaces, or Yarn Classic's array/object forms) is now recognized
(fixed in v0.1.46): a sibling workspace member named with an ordinary
semver range — not just pnpm's/Yarn Berry's explicit `workspace:`
protocol prefix, which this tool already skipped — resolves entirely
locally via a symlink and never reaches the registry. Confirmed live
(npm 9.2.0 and Yarn Classic 1.22.22 against a from-scratch two-package
workspace): npm's own `--loglevel silly` trace showed the dependency
placed from a local `file:` path with zero registry requests for that
name, and Yarn's verbose log showed it symlinking the local directory
instead. This is the default layout for Lerna/Nx/Turborepo monorepos,
not an obscure shape — npm's own monorepo (`npm/cli`) dogfoods it,
listing `@npmcli/docs`/`@npmcli/mock-registry`/`@npmcli/mock-globals`
as plain-semver `devDependencies` even though each is `"private": true`
and genuinely 404s on the public registry. Before this fix, every such
name was reported as a plain `not_found` hallucination.

## requirements.txt inline comments

A `requirements.txt` line with an explanatory trailing comment and no
version pin (e.g. `pandas    # used widely`, a common style — see
[nuPlan's](https://github.com/motional/nuplan-devkit/blob/master/requirements.txt)
for a real example) is parsed correctly: the comment is stripped the
same way `pip` itself treats it, and the dependency is still checked
(fixed in v0.1.3 — earlier versions silently dropped these lines
instead of checking them, which meant a hallucinated package name with
a trailing comment would never get flagged at all).

A comment that itself mentions a URL (e.g. `requests>=2.0  # docs:
https://requests.readthedocs.io`, common when a comment links to a
package's homepage or docs) no longer causes the whole line to be
mistaken for a direct-URL install and dropped (fixed in v0.1.5 —
earlier versions checked for `://` before stripping the comment, so
any dependency with a URL in its comment was silently skipped instead
of checked). A genuine direct-URL install (`-e https://...` or
`name @ https://...`) is still correctly skipped either way.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Support

If this caught something useful, a star helps others find it — that's
the main thing. This project is free and open source; if it's useful
to you, tips are also welcome via
[Liberapay](https://liberapay.com/experimental-gains/) or this ETH
address (self-custody, no KYC, no obligation):
`0x87053a1898994043e7476800cB5d4BDB423eADD7`

Build-in-public updates on [Nostr](https://njump.me/npub19ycp547pcykycy9kw3y04fe0wn3uukdukdhcdjdjce5s5ueg4qwq6un59y) (no account needed to read).

## License

MIT
