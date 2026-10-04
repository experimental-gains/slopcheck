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

An unpublished npm package now reports **not found**, not "recent" or
"ok" (fixed in v0.1.51). `registry.npmjs.org` keeps answering `GET
/<name>` with HTTP 200 for an unpublished package — the document
retains its original `time.created` and adds a `time.unpublished`
marker, but drops `versions` — while real npm tooling (`npm view`/
`npm install`) hard-fails with a 404 `"Unpublished on <date>"` for
that exact name. Confirmed live against a real unpublished package
(`@jinyezhao/hyness-plugins`, still 200 with a stale `time.created`
and no `versions`) and against the real `npm view` client, which
returned exactly that 404. Before this fix, slopcheck read only
`time.created`, so an unpublished package looked "recent" (unpublished
shortly after a fresh release) or, more dangerously, "ok" (unpublished
long after its original release) — both implying npm can still
install it when it no longer can.

`setup.cfg`'s `install_requires`/`[options.extras_require]` now follow
setuptools' own `file:` directive (fixed in v0.1.51). `install_requires
= file:requirements.txt` is real, current setuptools syntax — the
value doesn't name a package, it points at a file (or comma-separated
list of files, relative to the directory containing `setup.cfg`) whose
contents get read in and split into the real requirement list.
Confirmed against a real, currently-maintained PyPI package:
`CleanCut/green`'s shipped `setup.cfg` declares `install_requires =
file:requirements.txt` and `[options.extras_require] dev =
file:requirements-dev.txt`, both resolving (verified live with
setuptools' own `read_configuration`) to 9 real requirement names.
Before this fix, slopcheck handed the literal string
`"file:requirements.txt"` to its requirement-line matcher, which
doesn't match it (`:` isn't a valid name/version-specifier character),
so every dependency in the referenced file was silently never checked
— slopcheck reported "0 dependencies checked, all clean" against a
`setup.cfg` that setuptools itself resolves to real, live dependencies.

npm's `"gist:"` git-host shorthand is now recognized as a non-registry
dependency source, alongside the already-handled `"github:"`/
`"gitlab:"`/`"bitbucket:"`/bare-shorthand forms (fixed in v0.1.51).
npm's own docs list `gist:` as a fourth documented git-host prefix, and
npm-package-arg's `HostedGit.fromUrl()` dispatches it to git exactly
like the other three — but a real gist reference is just a bare gist
ID with no `/` at all (e.g. `gist:101a11beef`), so the existing
slash-based fallback that catches the other hosts' shorthand never
caught it. Confirmed live (npm 11.20.0): a hallucinated dependency name
paired with a `gist:`-prefixed version value made `npm install` run
`git ls-remote ssh://git@gist.github.com/<id>.git` and never contact
the npm registry at all, regardless of whether the gist exists. Before
this fix, slopcheck checked the literal name against the npm registry
and reported it "NOT FOUND" — a false positive on a legitimate, real
npm dependency shape.

A PyPI package whose every release has been yanked now reports **not
found**, not "ok" or "recent" (fixed in v0.1.52). PyPI's JSON API
keeps a per-file `yanked` flag but says nothing about installability
directly, and PEP 592 makes a real installer ignore every yanked
release unless a caller pins its exact version (`==`/`===`) — slopcheck
never sees or checks version pins at all (it only checks whether a
*name* exists), so it always models the unqualified `pip install
<name>` case, the one PEP 592 makes fail outright once every uploaded
file is yanked. Confirmed live with real pip 25.1.1 against a
from-scratch local index: a package with a single, fully-yanked
release made `pip install <name>` (no version pin) genuinely fail with
"No matching distribution found" — the identical failure shape as a
name that was never published — while pinning the exact yanked version
still installed it (with a warning). Before this fix, slopcheck read
only the upload timestamp and reported such a package "ok" or "recent"
depending on how old the yanked upload was, both implying an
installable package when an unqualified `pip install` genuinely can't
resolve one.

A PyPI project with a registered release version that has zero
uploaded files now reports **not found**, not "ok" (fixed in
v0.1.53). PyPI's JSON API can return HTTP 200 for a project with a
release entry like `"releases": {"0.0.0": []}` — a version exists but
nothing was ever uploaded for it — confirmed live against a real,
currently-registered project,
[`requests_extension`](https://pypi.org/pypi/requests_extension/json).
Real pip 25.1.1 fails an unqualified `pip install requests_extension`
outright with "Could not find a version that satisfies the
requirement ... (from versions: none)" / "No matching distribution
found" — the identical failure shape as a name that was never
registered at all. Before this fix, slopcheck flattened every
release's file list, found it empty, and fell straight into the
"can't determine an age, so ok" fallback without ever noticing there
was nothing installable to begin with. A project with no `releases`
key/dict at all is left alone (still "ok") — that shape doesn't appear
reachable for a genuine 200 response, since PyPI never creates a
project record without at least one completed release.

A `requirements.txt` line ending in a backslash now gets joined with
the line(s) that follow it before slopcheck tries to parse a
requirement out of it, the same way pip's own requirements-file reader
does (fixed in v0.1.54). `pip-compile --generate-hashes` (part of the
widely-used pip-tools) routinely wraps a spec too long for one line
this way — e.g. a long extras list pushed onto its own line, with the
version specifier continued on the next. Confirmed live with real pip
25.1.1: a two-line file reading `totally-hallucinated-xyz-987 \` then
`    ==1.2.3` made `pip install --dry-run -r` genuinely join them and
fail resolving the combined requirement — "Could not find a version
that satisfies the requirement totally-hallucinated-xyz-987==1.2.3
(from versions: none)" — the exact failure shape of a hallucinated
name. Before this fix, slopcheck's line-by-line reader had no
continuation-joining at all: the first line's trailing `\` doesn't
match any branch of the name-and-version regex, so it was silently
dropped, and the bare version-only continuation line never matches
either (no leading package-name character) — the dependency never got
checked, and slopcheck reported a clean scan for a file a real `pip
install -r` would genuinely fail on.

A Pipfile `[packages]`/`[dev-packages]` entry sourced via Pipenv's
`svn`/`hg`/`bzr` table keys is now correctly skipped as non-registry,
the same as the already-handled `git`/`path`/`file` keys (fixed in
v0.1.56). Pipenv's own `VCS_LIST` constant is `("git", "svn", "hg",
"bzr")`, and its schema accepts all four as equally valid dependency-
spec keys — confirmed live (Pipenv 2026.8.0): `pipenv lock -v` against
`totally-hallucinated-svn-test-xyz-123 = { svn =
"svn://127.0.0.1:9/repo" }` genuinely dispatched to pip's own
Subversion VCS backend and never queried PyPI for that name at all.
Before this fix, only `git`/`path`/`file` opted a Pipfile entry out of
the registry check, so an `svn`/`hg`/`bzr`-sourced dependency — a real,
still-current Pipenv feature — was sent to PyPI and reported as a
plain `not_found` hallucination unless the name happened to be
independently published there.

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
pip install git+https://github.com/experimental-gains/slopcheck.git@v0.1.96
```

## Usage

```bash
# scan the current directory (and every subdirectory) for
# requirements.txt / pyproject.toml / package.json / Pipfile / setup.cfg / pylock.toml
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
    pip install git+https://github.com/experimental-gains/slopcheck.git@v0.1.96
    slopcheck
```

## Use with pre-commit

```yaml
repos:
  - repo: https://github.com/experimental-gains/slopcheck
    rev: v0.1.96
    hooks:
      - id: slopcheck
```

Runs on any commit that touches `requirements.txt`, `pyproject.toml`,
`package.json`, `Pipfile`, `setup.cfg`, or `pylock.toml`. `pre-commit`
installs it into an isolated Python environment the first time (needs
Python 3.10+, no other setup).

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
| `requirements.in` (pip-tools) | PyPI |
| `pyproject.toml` (PEP 621 or Poetry) | PyPI |
| `Pipfile` (Pipenv) | PyPI |
| `setup.cfg` (setuptools) | PyPI |
| `pylock.toml`/`pylock.<name>.toml` (PEP 751 lock file) | PyPI |
| `environment.yml`/`environment.yaml` (conda, `pip:` section only) | PyPI |
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

A version value starting with a literal `.` — a bare `"."` or `".."`,
not just a multi-segment path like `"../foo"` (already caught by the
`/`-based check above) — is now also recognized as a local-path
reference (fixed in v0.1.63). npm-package-arg's own resolver checks
this (`isFilespec`, matching any value starting with `.`, `~/`, `/`, or
a drive letter) *before* it ever falls through to the git-host/slash
fallback above, so a bare `.`/`..` resolves locally even with no `/`
anywhere in it. Confirmed live end-to-end (npm 9.15.0, real `npm
install`, no `--dry-run`): a scratch `package.json` with
`"totally-hallucinated-selfref-xyz-987": "."` in `devDependencies`
installed cleanly — `node_modules/totally-hallucinated-selfref-xyz-987`
created, pointing back at the project's own directory — with zero
requests to `registry.npmjs.org` for that name anywhere in a full
`--loglevel silly` trace. Before this fix, slopcheck sent this name to
the public npm registry and reported it "NOT FOUND" — a false positive
on a dependency a real `npm install` resolves entirely locally.

`isFilespec`'s last alternative — a bare drive-letter prefix like
`"C:\Users\dev\local-lib"` or `"c:foo"` — is now recognized too (fixed
in v0.1.63). It matches unconditionally on every platform, not just
Windows: confirmed live (Linux, real npm 9.2.0) that a scratch
`package.json` dependency on `"C:\Users\dev\local-lib"` made npm
attempt to open `/C:/Users/dev/local-lib/package.json` and fail with
`ENOENT` — never a single request to `registry.npmjs.org` — the same
result for a lowercase, no-backslash `"c:foo"`. A two-letter prefix
like `"AB:foo"` does *not* match (confirmed live: npm takes a different,
still non-registry `EUNSUPPORTEDPROTOCOL` path for that one instead),
so the check requires exactly one leading ASCII letter before the
colon. Before this fix, slopcheck sent a Windows local-path override
committed as-is to the public npm registry and reported it "NOT
FOUND".

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

A `resolutions` entry's *value* — not just its key — is now checked when
it's itself an `npm:` alias (e.g. `"is-odd/**/is-number": "npm:totally-
hallucinated-xyz-42@1.0.0"` substitutes a completely different package for
the one the key names) (fixed in v0.1.55 — earlier versions only ever read
the pattern key, so an aliased entry checked the key-derived name — a real,
unrelated package in this example — and never the name Yarn actually fetches
from the registry). Confirmed live with Yarn Classic 1.22.22: `yarn install`
against exactly this shape genuinely queried
`https://registry.yarnpkg.com/totally-hallucinated-xyz-42` and failed with a
real 404, while the pre-fix key-derived name (`is-number`) was never even
looked up.

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

Poetry 2.0+ repurposes `[tool.poetry.dependencies]` for a project that
declares its dependencies in PEP 621's `[project.dependencies]`: a
same-named entry there now overrides that dependency's *source* (a
git/path/url table, same shape as the legacy form above) instead of
being a second, independent declaration. This override is now
recognized too (fixed in v0.1.59 — earlier versions still checked the
bare `[project.dependencies]` name against PyPI, ignoring the override
entirely). Confirmed live against real Poetry 2.5.1: a `pyproject.toml`
with `[project] dependencies = ["requests"]` plus
`[tool.poetry.dependencies] requests = { git = "..." }` made `poetry
lock` clone the git repo for `requests` and never once query PyPI for
that name.

Poetry 2.0+ does the identical by-name source-override merge one layer
over, for PEP 735 `[dependency-groups]` entries that share a name with a
`[tool.poetry.group.<name>]` table — this is now recognized too (fixed
in v0.1.89 — earlier versions only checked `[tool.poetry.dependencies]`
for this override, never `[tool.poetry.group.*.dependencies]`, so a
git/path/url source attached to a dependency-group entry this way was
ignored and the bare name was checked against PyPI instead). Confirmed
live against real Poetry 2.5.1: a `pyproject.toml` with
`[dependency-groups] test = ["some-package"]` plus
`[tool.poetry.group.test.dependencies] some-package = { git = "..." }`
made `poetry lock` clone the git repo and never once query PyPI for
that name.

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

`setup.cfg`'s own `[options.extras_require]` gets the identical
self-referential-extra treatment now too (fixed in v0.1.60) — the
pattern above isn't specific to PEP 621 syntax, it's a property of how
pip resolves a self-named `Requires-Dist` against the package already
being installed. Confirmed live (setuptools 84.0.0, real pip 25.x): a
from-scratch `setup.cfg`-only project (`[metadata] name =
totally-hallucinated-selfref-test-xyz-123`, `[options.extras_require]
all = totally-hallucinated-selfref-test-xyz-123[gui]`, `gui = pillow`,
no PEP 621 `[project]` table at all) had `pip install --dry-run -v
".[all]"` resolve the `all`/`gui` extras and install `pillow` without
ever issuing a single request for
`totally-hallucinated-selfref-test-xyz-123` itself. Before this fix,
`parse_setup_cfg` had no skip-set at all for this — unlike
`parse_pyproject_toml` — so a real `slopcheck` run against that same
project flagged it "NOT FOUND (no such project on PyPI)": a false
positive on a not-yet-published `setup.cfg`-based project using this
real, current pattern. Matched PEP 503-normalized, same as above;
`[options] setup_requires` is deliberately unaffected, since it
installs into an isolated build environment before the package's own
metadata/extras exist at all.

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

A differently-named `.txt`/`.in` requirements file is now also found by
a plain directory scan (`slopcheck`, no arguments), not just when named
directly on the command line (fixed in v0.1.88). The auto-discovery
walk only ever looked for the two literal filenames
`requirements.txt`/`requirements.in`, even though the paragraph above
already made passing one of these files *directly* work correctly —
so `home-assistant/core`'s real `requirements_test.txt` (test/lint
deps, not `-r`-included by its own `requirements.txt`) or
`cookiecutter-django`'s `requirements/base.txt` (its generated project
currently ships no top-level `requirements.txt` at all) were invisible
to `slopcheck .`/`slopcheck` with no arguments: either silently never
scanned (alongside another, unrelated manifest) or a hard "no manifest
found" error, depending on what else was in the directory. Scoped to
filenames/parent-directory names containing "requirement"
(case-insensitive) rather than every `.txt`/`.in` file in the tree, to
avoid feeding an unrelated prose file (a CHANGELOG, a wordlist fixture)
through the requirement-line parser.

The `-r`/`--requirement`/`-c`/`--constraint` directives above are now
recognized in every separator form pip's own `optparse`-based
requirements-file parser accepts, not just the spaced-out one (fixed in
v0.1.61) — `-rbase.txt` (no space between the short flag and its
argument) and `--requirement=base.txt` (long flag joined with `=`) are
both real, `optparse`-standard syntax pip genuinely resolves
identically to `-r base.txt`/`--requirement base.txt`. Confirmed live
against installed pip 25.1.1
(`pip._internal.req.req_file.parse_requirements`): both forms recursed
into the referenced file for real. Before this fix, the directive
regexes required literal whitespace after the flag, so either form fell
through completely unrecognized — not matched as `-r`, not matched as
`-c`, not matched as a plain dependency name either (the line still
starts with `-`) — and the whole line, along with every dependency
declared only in the file it pointed at, was silently dropped from the
scan with no error at all.

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

A `Pipfile` dependency sitting in a custom category — Pipenv's own
`pipenv install --categories` feature, any top-level table name other
than a fixed, Pipenv-internal exclusion list (`build-system`, `pipenv`,
`requires`, `scripts`, `source`) — is now read too (fixed in v0.1.91).
Reading Pipenv 2026.8.0's own source directly
(`pipenv/utils/pipfile.py`'s `Pipfile.get_package_categories`) shows it
treats every other top-level section as a genuine package category by
default, with no `--categories` flag needed for `pipenv lock` to
process it. Confirmed live: a `Pipfile` with a hallucinated name
sitting only in an arbitrary `[feature-x-packages]` table made a real
`pipenv lock` genuinely try and fail to resolve it from PyPI, the
identical failure as a name in `[packages]`. Before this fix, only the
two literal section names `packages`/`dev-packages` were read, so any
custom category's dependencies — a real, documented Pipenv feature for
splitting optional/feature-scoped dependency groups — were silently
never checked at all, the same "0 dependencies checked, all clean"
false-all-clear shape as every other manifest-coverage gap on this
list.

A [PEP 751](https://peps.python.org/pep-0751/) `pylock.toml` (or its
named variant, `pylock.<name>.toml`) is now recognized as a manifest
too (fixed in v0.1.64 — earlier versions had no filename entry for it
at all, and it's TOML, so the existing `*.txt` fallback for
oddly-named requirements files never caught it either). It's a real,
current lock-file format: pip 26.1 (April 2026) shipped experimental
`pip install -r pylock.toml` support, an alternative to `-r
requirements.txt`, and uv/PDM/Pipenv can already export to it.
Confirmed live against real pip 26.2.1: `pip lock -r req.txt -o
pylock.toml` produces the exact `[[packages]] name = "..." version =
"..." [[packages.wheels]] url = "https://files.pythonhosted.org/..."`
shape this parser reads, and a hand-added `[[packages]]` entry naming
a hallucinated package with no `vcs`/`directory`/`archive`/`sdist`/
`wheels` at all makes real `pip install -r` refuse the whole file
outright ("Exactly one of vcs, directory, archive must be set if sdist
and wheels are not set") before installing anything — while the same
entry with a fabricated wheel URL installs (or 404s) by that literal
URL alone, with zero query to the registry for the name, the same
non-registry-source shape already handled for Poetry's/uv's/Pipfile's
own git/path table forms, so entries sourced via `packages.vcs`/
`packages.directory` are skipped here too. Before this fix, a project
locked this way had every name in it — including any hallucinated
transitive dependency a real locker faithfully resolved and pinned
from the registry — silently never checked, the same "0 dependencies
checked, all clean" false-all-clear as the `Pipfile` gap above.

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

A single-line, semicolon-separated `setup.cfg` list value — e.g.
`install_requires = requests;totally-hallucinated-package-xyz-123` —
now has every entry checked, not just the first (fixed in v0.1.48).
setuptools' own list parser splits a value on `;` whenever the value
has no newline in it, and on `\n` when it does — confirmed live
against setuptools 84.0.0 (this project's own pinned minimum):
`setuptools.config.setupcfg.read_configuration` on that exact value
returns two real requirements. Earlier versions of this parser only
ever split on newlines, so a single-line semicolon list was treated as
one whole line and matched as a single requirement — the name before
the first `;` was extracted, and everything after it (including a
second, hallucinated package name) was silently swallowed the same way
a real PEP 508 environment marker (`; python_version < "3.8"`) is,
never even seen, let alone checked against PyPI.

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

A non-string entry in `[project.dependencies]`/`[project.optional-
dependencies]`, `[build-system] requires`, or either Hatch
dependency table no longer crashes the whole scan (fixed in v0.1.96).
None of those tables' schemas allow anything but a plain list of PEP
508 requirement strings, but TOML itself doesn't enforce that a list
stays homogeneous, so a hand-edited or LLM-written `pyproject.toml`
can genuinely put a bare number there without the TOML parser
objecting. Confirmed real and current: pypa/pip's own repository
ships `tests/data/src/pep518_invalid_requires/pyproject.toml` with
exactly `requires = [1, 2, 3]  # not a list of strings` — scanning a
real, unmodified checkout of `github.com/pypa/pip` crashed slopcheck
outright with `AttributeError: 'int' object has no attribute
'strip'` instead of a clean error. Real `pip install` on a project
with this table Fatals immediately with its own dedicated error
(`error: invalid-pyproject-build-system-requires`, "It is not a list
of strings") before resolving anything at all; the PEP 621
`[project.dependencies]` equivalent Fatals the same way one layer
deeper, inside the setuptools build backend. Before this fix, both of
`parse_pyproject_toml`'s raw-spec loops assumed every entry was
already a string and called `.strip()` on it unconditionally; now a
non-string entry raises the same clean, file-naming
`ManifestParseError` any other unparseable manifest already gets,
rather than an unhandled traceback.

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

Yarn Classic (v1) has a *third* private-registry mechanism, entirely
separate from both `.npmrc` and Yarn Berry's `.yarnrc.yml`: its own
`.yarnrc`, a plain (non-YAML) file of `key "value"` lines — exactly
what `yarn config set registry <url>` itself writes — with a bare
`registry "..."` line for a blanket override and a quoted
`"@scope:registry" "..."` line for a scoped one, plus a `YARN_REGISTRY`
env var equivalent of the blanket form (fixed in v0.1.57 — earlier
versions had no reader for either at all). Confirmed live with real
Yarn Classic 1.22.22: a scratch project with *only* a `.yarnrc`
containing `registry "http://127.0.0.1:9/"` — no `.npmrc`, no
`.yarnrc.yml` anywhere — made `yarn install --verbose` genuinely
perform a GET against `http://127.0.0.1:9/<name>` and fail with
`ECONNREFUSED`, never contacting `registry.yarnpkg.com` at all; the
scoped form routed only that scope's resolution the same way, and
`YARN_REGISTRY` (case-insensitively, the same as Yarn Berry's own env
vars) triggered the identical blanket override with no `.yarnrc` file
at all. The *value* must be double-quoted for Yarn Classic to honor it
— confirmed live that an otherwise-identical unquoted `registry
http://127.0.0.1:9/` line, and a single-quoted one, were both silently
ignored, falling straight through to the public registry — so this
tool only recognizes the double-quoted form real Yarn Classic itself
actually reads. `~/.yarnrc` is merged in as a home-directory global
config the same way as `.npmrc`/`.yarnrc.yml` (also confirmed live).
Before this fix, a Yarn-Classic-only project routing dependencies
through a private registry configured this way had every
legitimately-private dependency reported as a plain `not_found`
hallucination.

That `~/.yarnrc` global fallback wasn't quite right either (fixed in
v0.1.58): real Yarn Classic deliberately relocates its *global*
config-home directory away from the actual home directory whenever
it's running as root — confirmed by reading the real, installed
1.22.22 npm package's own bundled source — using `/usr/local/share`
instead (a `fakeroot(1)`-aware check, so a `fakeroot`-wrapped process
still gets the real home). Confirmed live running actual Yarn Classic
1.22.22 as root: a blanket `registry "..."` placed *only* in
`/usr/local/share/.yarnrc` — no project `.yarnrc`, no `$HOME/.yarnrc`
at all — was genuinely honored by a real `yarn install`. Since root is
an extremely common way to run both real installs (Docker/CI base
images) and slopcheck itself, `~/.yarnrc` alone silently missed this
global config before this fix.

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
A dependency's own `index = "<name>"` scoping key is now read from
every Pipenv category section, not just `packages`/`dev-packages`
(fixed in v0.1.91, alongside the manifest-coverage fix above) — a
dependency living only in a custom category (e.g.
`[feature-x-packages]`) with its own `index=` key is genuinely resolved
against that scoped source by a real `pipenv lock`, confirmed live the
same way as the single-category case.

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

pnpm's own workspace membership is now recognized too (fixed in
v0.1.65): pnpm declares it an entirely different way from npm/Yarn
Classic above — a separate `pnpm-workspace.yaml` file at the workspace
root with its own `packages:` glob list, never `package.json`'s
`workspaces` field at all. Confirmed live (pnpm 9.15.0) with
`link-workspace-packages=true` set in a root `.npmrc` — a real,
documented pnpm setting (https://pnpm.io/settings#linkworkspacepackages)
and the default before pnpm 8, so still commonly carried over into
older or migrated monorepos' checked-in config: a root manifest's
plain-semver-range dependency on a private, unpublished sibling
resolved entirely locally, with zero registry requests for that name.
Before this fix, `pnpm-workspace.yaml` wasn't read at all, so the same
shape was reported as a plain `not_found` hallucination.

pnpm's `overrides` field (https://pnpm.io/settings/dependency-resolution#overrides)
is now also recognized when it's set directly in `pnpm-workspace.yaml`
(fixed in v0.1.75) — not just under `package.json`'s `pnpm.overrides`,
already handled above. pnpm's own docs show this exact field living at
the project root in `pnpm-workspace.yaml` instead. Confirmed live (pnpm
12.8.1): `overrides: { is-number: 'npm:totally-hallucinated-pnpm-ws-
override-alias-xyz-321@1.0.0' }` — with `is-number` a real transitive
dependency of `is-odd`, named in no `package.json` at all — made `pnpm
install` genuinely issue `GET https://registry.npmjs.org/totally-
hallucinated-pnpm-ws-override-alias-xyz-321` and fail with a real 404;
`is-number` itself was never fetched under its own name once
overridden. An override key using pnpm's own `"parent@version>
dependency"` scoping syntax is resolved to the real dependency name
(the segment after the last `>`), and a literal `"-"` value (pnpm's
documented "remove this dependency" syntax) is correctly skipped —
real pnpm never fetches a removed dependency. Before this fix,
`pnpm-workspace.yaml`'s entire body was discarded unconditionally (the
file was only recognized for its `packages:` workspace-membership
role), so a hallucinated name routed through its `overrides` field was
invisible no matter what.

A pnpm workspace member scanned on its own now also discovers its
workspace root's `pnpm-workspace.yaml`, the same way the PDM workspace
fix above does for `[tool.pdm.workspace]` (fixed in v0.1.93). pnpm
declares workspace membership in a file that only ever lives at the
monorepo root, never inside a member's own directory — so scanning just
one member (a realistic shape: a monorepo CI job or pre-commit hook
scoped to one changed package) never saw `pnpm-workspace.yaml` at all,
the exact same gap already fixed for npm/Yarn Classic, uv, and PDM's own
workspace-root discovery, just never ported to pnpm's. Confirmed live
(pnpm 12.8.1, a from-scratch two-package workspace: root
`pnpm-workspace.yaml` with `packages: ['packages/*']` and
`linkWorkspacePackages: true` — note this setting is read from
`pnpm-workspace.yaml` itself in current pnpm, not a root `.npmrc` the
way an older pnpm accepted, confirmed by comparing both locations
live): running `pnpm install` from inside the member directory itself
genuinely resolved a sibling member's plain-semver-range dependency
entirely locally — a real symlink into `node_modules`, zero
`registry.npmjs.org` requests for that name in a `--loglevel debug`
trace. Before this fix, scanning just the member directory reported the
sibling's name a plain `not_found` hallucination; scanning the whole
tree (so the root's `pnpm-workspace.yaml` was itself among the scanned
files) already worked correctly.

A `pnpm-workspace.yaml` with a trailing `#` comment — on the `packages:`
header line itself, or on an individual glob — is now parsed correctly
(fixed in v0.1.95). YAML allows a comment anywhere a value isn't being
spelled out, and real `pnpm-workspace.yaml` files use this to annotate
either spot. Before this fix, a comment on the header line
(`packages:  # list of workspace globs`) made the whole block read as a
non-list inline value, silently dropping every pattern inside it; a
comment on a glob line (`- 'packages/*'  # keep in sync with CI
matrix`) folded the comment text into the pattern itself, so it never
matched any real directory. Either shape made a real workspace member's
dependency on a sibling package go unrecognized and reported as a
fabricated `not_found` hallucination instead of resolved locally.
Confirmed live by direct repro against both shapes.

A conda `environment.yml`/`environment.yaml` is now recognized as a
manifest too (fixed in v0.1.66 — earlier versions had no filename entry
for either at all). conda's own documented "mixed" format lets a
top-level `dependencies:` block sequence combine conda-channel packages
(`python=3.8.5`, `pytorch=1.11.0`, `cudatoolkit=11.3` — not PyPI
releases, and not checked here) with a nested `pip:` list of ordinary
PyPI package specs, real and common in ML/data-science repos that need
a CUDA-specific conda build of a package alongside PyPI-only
dependencies. Confirmed against a real, currently-used file,
[`CompVis/latent-diffusion`'s `environment.yaml`](https://github.com/CompVis/latent-diffusion/blob/main/environment.yaml),
and against conda's own source (`conda/env/installers/pip.py`,
`install()`): every `pip:` entry is written verbatim into a temporary
`requirements.txt` and installed via a real `pip install -U -r
<tmpfile>` subprocess. Before this fix, a conda-only project's entire
PyPI dependency list — hallucinated names included — was silently
never scanned; running `slopcheck` against a directory containing only
an `environment.yml`/`environment.yaml` hit the "no manifest found"
error, or worse, silently reported "0 dependencies checked, all clean"
whenever any other supported-but-dependency-free manifest (a
`pyproject.toml` with only `[tool.*]` config, say) happened to sit
alongside it.

A `pip:` entry that's itself a nested `-r`/`--requirement` directive
(e.g. `- -r requirements-dev.txt`, splitting main/dev pip deps across
files the same ordinary way a standalone `requirements.txt` project
already can) is now followed too (fixed in v0.1.67 — v0.1.66 only
handled `-e`/URL entries, not this one). conda's own `install()` writes
the `pip:` list into a temp requirements file inside
`get_pip_workdir(args.file)` — confirmed by reading that function
directly, this is the `environment.yml`'s own directory, not a
throwaway tmpdir — and runs `pip install -U -r <tmpfile>` with that
same directory as the working directory, so real pip's own
requirements-file parser recurses into a `-r`/`--requirement` target
exactly like it would for a standalone `requirements.txt`. Verified
end-to-end with a real Miniforge/conda 26.7.2 install: an
`environment.yml` whose `pip:` list was just
`- -r requirements-dev.txt`, with a sibling `requirements-dev.txt`
naming a hallucinated package, made `conda env create` genuinely try
(and fail) to `pip install` that name. Before this fix,
`parse_environment_yml` matched each `pip:` line against its
requirement-line regex directly with only an `-e `/`--`/`://` skip
inlined by hand — a bare `-r requirements-dev.txt` line matches neither
that skip nor the regex (no leading name character), so it was
silently dropped and the referenced file's dependencies were never
checked at all.

pip-tools' own `requirements.in` is now recognized as a manifest too
(fixed in v0.1.68 — earlier versions had no filename entry for it at
all, and no suffix fallback the way other `.txt` names get). `pip-compile`
(part of pip-tools) parses `requirements.in` with pip's own
requirements-file parser — the exact same entry point real pip itself
uses for a plain `requirements.txt` — so it's genuinely the identical
format (`-r`/`-c`/`-e`, inline comments, backslash continuations, all of
it), just under pip-tools' own, equally conventional filename for the
hand-edited "source" file a human (or an LLM coding assistant) actually
touches, compiled into the fully-pinned `requirements.txt` this project
already read. Before this fix, running `slopcheck` against a directory
containing only a `requirements.in` (a real state — reviewing a change
before the compiled `.txt` is regenerated, or a repo that doesn't commit
the compiled artifact) hit the "no manifest found" error, or worse,
silently reported "0 dependencies checked, all clean" whenever any other
supported-but-dependency-free manifest (a `pyproject.toml` with only
`[tool.black]` config, say) happened to sit alongside it — the exact
same silent false-all-clear shape already fixed here for `Pipfile`/
`setup.cfg`/`environment.yml`. A non-canonical `.in` filename (e.g.
`requirements-dev.in`, the pip-tools analog of `requirements_test.txt`)
is handled the same way `requirements.txt`'s own non-canonical `.txt`
names already are: works when scanned directly, or reached via
`-r`-recursion, even though it isn't auto-discovered by a bare directory
scan.

A `pip:` entry spread across a backslash continuation (the same
`pip-compile --generate-hashes` shape already handled for a standalone
`requirements.txt` since v0.1.54) is now joined before parsing (fixed
in v0.1.94). conda's `install()` writes each `pip:` YAML list entry
into that same temp requirements file one physical line at a time, and
the real `pip install -U -r` subprocess it runs is subject to pip's
own backslash-continuation joining regardless of which file pip is
reading — confirmed live with real pip 25.1.1: a two-line `pip:` entry
(`totally-hallucinated-xyz-987 \` then `==1.2.3`) genuinely joins into
one requirement and fails resolving it from PyPI. Before this fix,
`parse_environment_yml` and the `pip:`-block file-discovery path never
applied the join the standalone-requirements.txt path already got in
v0.1.54 — a spec wrapped onto a continuation line matched neither half
of the join-aware regex, so it was silently dropped instead of checked.

A trailing `#` comment on the `dependencies:`/`pip:` header lines of an
`environment.yml`/`environment.yaml` is now handled correctly too
(fixed in v0.1.95 — the `environment.yml` sibling of the
`pnpm-workspace.yaml` header-comment fix above, same root cause). A
comment on either header line (e.g. `dependencies:  # conda + pip mix`
or `pip:  # pypi-only extras`) made the key-match check read as
non-empty/non-matching, so the entire nested `pip:` block was wrongly
treated as absent — every PyPI name inside it, hallucinated or not, was
silently never checked at all, the same false-all-clear shape already
fixed once for this file in v0.1.66. Confirmed live by direct repro.

A `-i`/`--extra-index-url`/`--index-url` directive in a `requirements.in`
file (pip-tools' own hand-edited source file, added as a recognized
manifest in v0.1.68 above) is now honored the same way one already is in
a `requirements.txt` (fixed in v0.1.69). The scan for this directive was
still filtered to `.txt`-suffixed paths only — a filter written back
when `requirements.txt` was the only requirements-format filename this
tool recognized, and never revisited when `requirements.in` got its own
manifest entry one release earlier. `requirements.in` is, if anything,
*more* likely to carry the actual directive than the compiled `.txt`,
since it's the file a human (or an LLM coding assistant) edits by hand —
`pip-compile` just carries it forward into the generated file from
there. Live-verified: identical content (an `-i` line plus a
hallucinated package name with nothing else naming it) was correctly
downgraded to `private` when saved as `requirements.txt`, but reported
as a plain `not_found` hallucination when saved as `requirements.in`,
purely because of this suffix filter.

PDM's own workspace feature (https://pdm-project.org/latest/usage/workspace/,
added in PDM 2.28.0) is now recognized too (fixed in v0.1.70): a *third*
monorepo-membership mechanism, declared in the root project's
`[tool.pdm.workspace] members = [...]` and structurally closer to pnpm's
than to uv's — a member is referenced with an ordinary `dependencies =
["bar"]` entry, no `[tool.pdm.sources]` table needed at all (unlike uv's
`[tool.uv.workspace]`, which requires a matching `[tool.uv.sources] name =
{ workspace = true }` entry before a plain dependency resolves locally —
confirmed live, real `uv lock` Fatals without it: "is included as a
workspace member, but is missing an entry in tool.uv.sources"). Confirmed
live (PDM 2.29.2, `pdm lock -v` against a from-scratch two-project
workspace with no `[tool.pdm.sources]` anywhere): the dependency resolved
entirely locally ("The file packages/bar is a local directory, use it
directly") with zero PyPI requests. Before this fix, `[tool.pdm.workspace]`
wasn't read at all, so a real PDM workspace's own private, unpublished
sibling package — referenced exactly the way PDM's own docs show — was
reported as a plain `not_found` hallucination.

A PDM workspace member scanned on its own now also recognizes its
*sibling* members' names, not just the ones declared in a scanned root
(fixed in v0.1.90). `[tool.pdm.workspace].members` only ever lives in
the root's pyproject.toml, so scanning just a member directory — the
same monorepo-CI shape as the npm/pnpm/uv workspace-root fixes
elsewhere on this page — never discovered the root at all, and
therefore never saw any sibling's declared name as locally resolved.
Confirmed live (PDM 2.29.2): a member depending on another member by
name resolved entirely from the local checkout ("The file
packages/other is a local directory, use it directly") when `pdm lock`
ran from the root — the only way PDM allows it to run at all. Before
this fix, slopcheck scanning just the dependent member's own directory
reported the sibling's name a plain `not_found` hallucination, even
though scanning the whole tree (so the root's pyproject.toml was also
among the scanned manifests) already correctly recognized it.

Hatch's own `extra-dependencies` field on a named
`[tool.hatch.envs.<name>]` table (https://hatch.pypa.io/latest/config/environment/overview/#dependencies)
is now read too (fixed in v0.1.71): it lets an environment that inherits
from another (implicitly from `default`) add packages on top of the
inherited `dependencies` list without redeclaring it, and Hatch's own
`environment_dependencies_complex` resolves it through the exact same
validation and install path as `dependencies` itself. Confirmed live
(Hatch 1.18.1): a scratch project with `[tool.hatch.envs.experimental]
extra-dependencies = ["totally-hallucinated-hatch-extradep-xyz-123"]`
made `hatch env create experimental` genuinely fail resolving the fake
name from PyPI ("Could not find a version that satisfies the
requirement ... (from versions: none)"). Before this fix, `_hatch_deps`
only ever read `dependencies`, so this sibling field on the same table
was silently never checked at all.

A `-i`/`--extra-index-url`/`--index-url` directive placed directly in a
conda `environment.yml`/`environment.yaml`'s own `pip:` block is now
honored too (fixed in v0.1.72). Confirmed reading conda's own
`conda/env/installers/pip.py` `install()` directly (github.com/conda/conda's
`main` branch): every `pip:` entry is written *verbatim* — no filtering of
any kind — into a temp requirements file, which a real `pip install -U -r
<tmpfile> --exists-action=b` subprocess then reads, so a directive sitting
in that list is exactly as effective as the same line at the top of a
standalone `requirements.txt` (live-verified separately with real pip: a
requirements file whose first line is `-i http://<private-index>/simple`
genuinely directs pip's lookup there instead of PyPI). Before this fix, the
private-index scan only ever read `.txt`/`.in`-suffixed file *paths* —
environment.yml's own YAML body was never one of those paths, so a
directive living inline in its `pip:` block (or reached indirectly via a
nested `-r other.txt` line inside that same block) was invisible to it no
matter how that suffix filter was widened. Live-verified: identical content
(the same directive plus the same hallucinated name) was correctly
downgraded to `private` when saved as `requirements.txt`, but reported as a
plain `not_found` hallucination when saved inline in an environment.yml
`pip:` block.

A `-r`/`-i`/`-e`/`-c` pip-style directive line inside a `setup.cfg`/PEP 621
`file:`-referenced requirements file is no longer followed or treated as a
dependency (fixed in v0.1.73). setuptools' own `file:` directive
(confirmed reading setuptools 84.0.0's `setupcfg.py`/`pyprojecttoml.py`)
is *not* a real pip requirements-file reader: it reads the referenced
file's raw text and splits it on newline/`;` exactly like a plain,
non-`file:` list value — there's no `-r` recursion step, and a `-i`/`-e`/
`-c` line isn't skipped as a directive, it's kept as a literal string and
handed straight to `packaging.requirements.Requirement()` at build time.
Live-verified: a real `pip install .`/`python -m build` against a
`setup.cfg` (or a PEP 621 `[tool.setuptools.dynamic]` equivalent) whose
`file:`-referenced requirements file contains `-r other.txt` Fatals
immediately with `InvalidRequirement: Expected package name at the start
of dependency specifier`, before a single dependency — including an
innocent sibling requirement on the next line, or anything in the
referenced `-r` target — is ever resolved. Before this fix, the `file:`
target was read with the real pip requirements-file parser, which
genuinely follows a `-r other.txt` line and reports whatever's in there as
an ordinary dependency of the project — actively misleading, since the
real tool never gets far enough to resolve (or even attempt to resolve)
that name at all; the real, actionable problem (the malformed `-r` line
itself, which breaks the build outright) was never surfaced.

Scanning a single npm/Yarn-Classic workspace *member* directory on its
own — not the monorepo root, e.g. a CI job or pre-commit hook scoped to
one changed package — now also checks the enclosing workspace root's
`.npmrc`/`.yarnrc` (fixed in v0.1.78). Confirmed live (npm 9.2.0, Yarn
Classic 1.22.22): running `npm install`/`yarn config get` from inside a
workspace member genuinely loads the workspace root's `.npmrc`/`.yarnrc`
too — npm's debug log shows `info found workspace root at ...` followed
by `config:load:project` reading that directory, and Yarn's verbose log
walks every ancestor checking for `.yarnrc`/`.npmrc` — even though the
member directory's own `package.json` is the only manifest in scope. A
plain nested `package.json` with no enclosing `workspaces` field does
*not* get this treatment (confirmed live the same way: npm then treats
it as its own independent project root and never reads the parent's
`.npmrc` at all), so the fix only climbs to an ancestor whose
`workspaces` glob patterns actually resolve the scanned directory as a
member. Before this fix, `_npmrc_paths`/`_yarn_classic_rc_paths` only
ever checked `.npmrc`/`.yarnrc` in the exact directory holding the
scanned `package.json`, so a dependency a real `npm install`/`yarn
install` would resolve against the workspace root's configured private
registry was misreported as a plain `not_found` hallucination instead
of downgraded to `private`.

pnpm declares workspace membership an entirely different way — a
sibling `pnpm-workspace.yaml`, not `package.json`'s `workspaces` field
— so the npm/Yarn-Classic fix just above never covered it: scanning a
pnpm workspace member directory on its own missed the enclosing
monorepo root's `.npmrc` entirely (fixed in v0.1.93). pnpm is still
npm-registry-compatible and reads the exact same `.npmrc` hierarchy —
confirmed live (pnpm 12.8.1): `pnpm config get <scope>:registry`, run
from inside a workspace member directory with no `.npmrc` of its own,
genuinely resolved a scope mapping declared only in the enclosing
`pnpm-workspace.yaml` root's `.npmrc`; the identical command run from a
sibling directory the root's `packages:` glob does *not* match
returned `undefined`, confirming it's a real member-match rule, not
just "any ancestor `.npmrc`". Before this fix, a dependency a real
`pnpm install` would resolve against the root-configured private
registry was misreported as a plain `not_found` hallucination instead
of downgraded to `private`.

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

`setup.cfg`'s `install_requires`/`setup_requires`/`[options.extras_require]`
(and the `file:`-directive files they can point at) use a *different*,
stricter comment rule than the one above, matching real setuptools exactly
rather than pip's: only a trailing comment preceded by a single literal
space is stripped; one preceded by a tab (an ordinary tab-aligned-comment
editor habit) is left glued onto the requirement string, since that's what
real setuptools itself does (`jaraco.text.drop_comment`, confirmed reading
its vendored source: `line.partition(' #')[0]`) before handing it to
`packaging.requirements.Requirement()` (fixed in v0.1.74 — earlier versions
reused the pip-style "any whitespace before `#`" rule here too, so a
tab-preceded comment was silently stripped and the pre-comment name
reported as an ordinary, clean dependency, even though a real
`pip install .`/`python -m build` genuinely Fatals with
`InvalidRequirement` on that exact line before resolving any dependency at
all, hallucinated or not).

A `pylock.toml` `[[packages]]` entry whose `archive`/`sdist`/
`[[packages.wheels]]` table names its file with a `path` key instead of a
`url` key (PEP 751 supports either) is no longer checked against PyPI
(fixed in v0.1.76 — earlier versions only excluded `vcs`/`directory`
sources, so this shape slipped through to the ordinary registry check).
Confirmed live with uv 0.12.19: pinning a dependency via
`[tool.uv.sources]`'s file-path form (one exact local wheel, not a source
directory) and running `uv export --format pylock.toml` produced an
`archive = { path = "...", hashes = {...} }` table with no `url` anywhere
in the entry — uv never queried PyPI for that name — while the pre-fix
parser still sent it to the registry check and reported it `not_found`, a
false "hallucinated" positive on a package no real locker tool ever
resolved from the index.

Yarn Berry's own `exec:` protocol (https://yarnpkg.com/features/protocols#exec)
is now recognized as a non-registry dependency source too (fixed in
v0.1.77). It builds a `package.json` dependency on the fly from a local
script instead of fetching it from any registry — a real descriptor is
just a relative path to that script, and unlike the `github:`/`gitlab:`/
`bitbucket:` shorthand forms already handled, it needs no `/` anywhere
when the script sits right next to `package.json` itself (a bare filename
like `exec:builder.js` is syntactically valid), so the existing
slash-based fallback never caught it. Confirmed live (Yarn Berry 4.18.1,
`yarn install` with scripts enabled): a scratch `package.json` with a
sibling `builder.js` and `"totally-hallucinated-execprotocol-xyz-556":
"exec:builder.js"` failed resolution with `...@exec:builder.js...:
Manifest not found` — a purely local-filesystem error raised before
Yarn's install ever reaches its Fetch step — and made zero requests to
`registry.yarnpkg.com`/`registry.npmjs.org` for that name. Before this
fix, slopcheck sent the literal name to the npm registry and reported it
a plain `not_found` hallucination — a false positive on a dependency a
real `yarn install` never asks the registry about at all.

npm's `overrides` (and its `pnpm.overrides` alias), pnpm-workspace.yaml's
own top-level `overrides:`, and Yarn's `resolutions` no longer check a
name against the registry when its override/resolution *value* is a
`file:`/git-URL/tarball-URL specifier instead of a version, range, or
`npm:` alias (fixed in v0.1.79). All three real mechanisms accept such a
value — npm's own docs list "an exact version, a semver range, a dist-tag,
or a replacement specifier such as `npm:`, `file:`, or a Git URL" — and
when it's used, the package is fetched straight from that location,
never touching the registry for its name at all. Confirmed live across
all three tools: npm 12.2.0 (`{"wrappy": "file:./local-fork"}`, `npm
install --dry-run -v` fetched only the other direct dependency, never
`wrappy`), pnpm 12.8.1 (`overrides: {wrappy: 'http://127.0.0.1:8911/
wrappy-1.0.2.tgz'}`, zero requests to `registry.npmjs.org` for it), and
Yarn Classic 1.22.22 (`"resolutions": {"wrappy": "file:./local-fork"}`,
resolved straight from the path). A real example of this shape in active
use: withastro/astro's own `pnpm-workspace.yaml` pins `'docs>
@lunariajs/core'` to a `pkg.pr.new` tarball URL today. Before this fix,
the override/resolution name was always checked regardless of the
value's shape, so a legitimate fork/local-path/private-tarball override
on any of the three mechanisms was reported as a hallucinated `not_found`
package.

A PyPI package's "recent" age is now computed from its newest-released
*non-yanked* file only, not the oldest file ever uploaded regardless of
yanked status (fixed in v0.1.80). PEP 592 makes a real unqualified `pip
install <name>` ignore every yanked file entirely (not just prefer a
non-yanked one when there's a tie) — slopcheck already knew this well
enough to report `not_found` when *every* release is yanked (v0.1.52),
but a mix of yanked and non-yanked releases still computed the "how long
has this existed" age from the single oldest upload across *all* files,
yanked ones included. Confirmed live (pip 25.1.1, a from-scratch local
index: `testpkg-1.0.0` marked `data-yanked`, `testpkg-1.0.1` not):
`pip install --dry-run -i <index> testpkg` downloads and would install
only `1.0.1`, never even considering `1.0.0`. A project whose only old
release was yanked years ago and whose sole real, installable release
was published days ago — exactly the shape a reused or taken-over PyPI
project name can produce — used to report "ok" (the ancient yanked
file's date won the `min()`), implying a long-established, installable
package, when a real `pip install <name>` today resolves straight to the
brand-new release and nothing else. Before this fix there was no
distinction at all between a yanked file's upload date and a real one's
for this calculation.

pip's `-f`/`--find-links` (a flat file/HTML-page/local-directory source
of archives, searched *in addition to* the configured index) is now
recognized as a private/extra source, in every spelling pip itself
accepts: a requirements.txt line (`-f /path`, `-f/path`, or
`--find-links=/path`), the `PIP_FIND_LINKS` env var, and `pip.conf`'s
`find-links`/`find_links` (fixed in v0.1.81). Confirmed live (pip
25.1.1): a real wheel built for a never-published name and dropped in a
throwaway local directory let `pip install --dry-run -r` genuinely find
and "Would install" it via `-f`, while the public PyPI JSON API 404'd
the same name throughout — a real pattern for airgapped CI or a
vendored-wheels directory committed alongside a project. Before this
fix, slopcheck had no notion of `-f`/`--find-links` at all, so a
dependency resolvable this way reported as a plain `not_found`
hallucination instead of downgrading to `private`, the same
misclassification already fixed for `-i`/`--extra-index-url`.

uv's own `find-links` setting — its analog of pip's `-f`/`--find-links`
above, a list of flat file/HTML-page/local-directory sources searched
alongside any configured index — is now recognized too, however it's
set: the `UV_FIND_LINKS` env var, a `find-links = [...]` key in a
standalone `uv.toml`, or the same key in pyproject.toml's `[tool.uv]`
table (fixed in v0.1.82). Confirmed live (uv 0.12.19): a hand-built
wheel for a never-published name, dropped in a throwaway local
directory and named only via `find-links`, made a real `uv lock`
genuinely resolve and lock it, while the same name failed with "was not
found in the package registry" against real PyPI with no `find-links`
configured. Before this fix, `uv_private_registry_context` checked only
uv's `[[index]]`/`[[tool.uv.index]]` tables and their env var
equivalents, so a project relying solely on `find-links` had every
genuinely-resolvable dependency it named reported as a plain `not_found`
hallucination instead of downgraded to `private` — the same
cross-ecosystem gap already closed for pip just above.

Hatch's `overrides` table is now read too (fixed in v0.1.83): on top of
the plain `dependencies`/`extra-dependencies` fields a named
`[tool.hatch.envs.<name>]` already carries, Hatch lets that same
environment inject *more* dependencies via
`overrides.<source>.<condition>.dependencies`/`extra-dependencies`,
where `source` is one of `platform`/`env`/`matrix`/`name`
(https://hatch.pypa.io/latest/config/environment/advanced/#overrides).
Confirmed live (Hatch 1.18.1): a scratch project with
`[tool.hatch.envs.test.overrides] matrix.pyver.dependencies = [{value =
"totally-hallucinated-hatch-override-xyz-999", if = ["a"]}]` made `hatch
env create test.a` genuinely fail resolving the fake name from PyPI
("Could not find a version that satisfies the requirement
totally-hallucinated-hatch-override-xyz-999 (from versions: none)"),
while `hatch env create test.b` (the matrix variant whose `if` condition
doesn't match) installed cleanly — confirming the entry is genuinely
condition-gated at runtime. slopcheck deliberately doesn't try to
evaluate that gating itself (it can't know which platform/env/matrix
variant a real invocation will use), so every entry under every
source/condition is checked unconditionally. Before this fix,
`_hatch_deps` never read `overrides` at all, so a hallucinated name
planted there — invisible in the environment's own plain `dependencies`
list — sailed through unchecked even though a real `hatch env create`
genuinely tries to install it.

PDM's own config files/env var for its default PyPI index are now read
too (fixed in v0.1.84) — a seventh private-registry mechanism, entirely
separate from the `[[tool.pdm.source]]` array in pyproject.toml this
tool already handled: `pdm config pypi.url <url>` (or `pdm config
pypi.<name>.url <url>` for an additional named source) writes to a
project-local `pdm.toml`/legacy `.pdm.toml`, a per-user global
`config.toml`, or a site-wide one — none of which is pyproject.toml —
and `PDM_PYPI_URL` is `pypi.url`'s own documented env var equivalent.
Confirmed live (PDM 2.29.2), with zero `[[tool.pdm.source]]` entries
anywhere: a committed `pdm.toml` with `[pypi] url =
"http://127.0.0.1:9/simple"` made `pdm lock` genuinely attempt that
address instead of pypi.org (`ConnectError: Connection refused`), and
the identical setting written only to `~/.config/pdm/config.toml` via
`pdm config -g pypi.url ...`, or set only via `PDM_PYPI_URL`, was each
honored the same way with no project-level config at all. Before this
fix, a PDM project routing its default index through any of these (a
real, `pdm config`-documented pattern — e.g. a CI image or
dotfiles-managed machine baking in a company mirror once instead of
repeating it per project) had every genuinely-resolvable private-only
dependency reported as a plain `not_found` hallucination instead of
downgraded to `private`.

A PDM workspace member scanned on its own now inherits its workspace
root's `[[tool.pdm.source]]`/`pdm.toml`/`config.toml`/`PDM_PYPI_URL`
config too (fixed in v0.1.90) — the PDM analog of the uv fix right
below. Confirmed live (PDM 2.29.2): `pdm lock`/`pdm install` both
hard-error with "can only be run from the workspace root" when invoked
from inside a member directory at all, so the *only* real resolution
that ever happens is from the root — and running from there genuinely
attempted a root-level `[[tool.pdm.source]]`'s unreachable address for
a dependency declared only in a member's own `[project.dependencies]`,
with no source table of its own (`pdm.termui: Adding requirement
<name>(from member 0.1.0)`, then `ConnectError` to the configured
address). Before this fix, `pdm_private_registry_context` only ever
looked at the directory holding each *scanned* pyproject.toml, so
scanning just a workspace member directory — the same realistic
monorepo-CI shape as every other workspace-root fix on this page —
never saw the root's private-index config at all, reporting the
dependency a plain `not_found` hallucination instead of downgraded to
`private`.

A uv workspace member scanned on its own now inherits its workspace
root's private-index config too (fixed in v0.1.85). uv workspaces
(https://docs.astral.sh/uv/concepts/projects/workspaces/) resolve as one
shared unit: confirmed live (uv 0.12.19), a two-level layout
(`root/pyproject.toml` with `[tool.uv.workspace] members = ["pkgs/*"]`
plus a `[[tool.uv.index]]` entry pointing at an unreachable
`127.0.0.1:9`, and `root/pkgs/foo/pyproject.toml` with no uv config of
its own, naming a fake dependency) made `uv lock` run from *inside*
`pkgs/foo` genuinely discover the workspace root and attempt to resolve
the fake dependency against that root-configured index, never
contacting PyPI — the same thing an explicit index declared only in the
root and referenced via a member's own `[tool.uv.sources]` does too.
Before this fix, `uv_private_registry_context` only ever looked at
`uv.toml`/`[tool.uv]` in the directory holding each *scanned*
pyproject.toml, so scanning just a workspace member directory — a
realistic shape, e.g. a monorepo CI job scoped to one changed package,
the same pattern already fixed for npm/Yarn workspaces — never saw the
workspace root's index config at all (it isn't even among the scanned
manifests), misreporting a genuinely-resolvable dependency as a plain
`not_found` hallucination instead of downgraded to `private`. A
directory excluded from the workspace via `[tool.uv.workspace].exclude`
correctly does *not* get this treatment, confirmed live the same way.

Pipenv's `PIPENV_PYPI_MIRROR` environment variable is now recognized too
(fixed in v0.1.86) — a *fifth* mechanism for a `Pipfile`-based project to
resolve beyond public PyPI, entirely outside the `Pipfile` itself and
separate from the `[[source]]` table this tool already handled. Reading
Pipenv's own source (`pipenv/utils/sources.py`'s `pipfile_sources`): when
set, this env var overwrites the `url` of every source whose current
`url` is public PyPI — including the implicit built-in default used when
a `Pipfile` has no `[[source]]` table at all. Confirmed live (real
`pipenv lock` under Pipenv 2026.8.0, two arrangements, watching for a
`127.0.0.1:9` connection attempt): a `Pipfile` with the conventional
`name = "pypi", url = "https://pypi.org/simple"` as its only source, and
a `Pipfile` with no `[[source]]` table at all, both genuinely routed an
undecorated dependency's resolution to the mirror's address instead of
pypi.org — the same mirror/caching-proxy pattern pip's `PIP_INDEX_URL`
and PDM's `PDM_PYPI_URL` already get folded into this tool's blanket
signal. Before this fix, neither case was recognized, so a Pipenv
project run with this env var set (a real, documented pattern — an
offline/airgapped CI image or a company-wide caching mirror baked into
the environment rather than repeated per project) had every
genuinely-resolvable dependency reported as a plain `not_found`
hallucination instead of downgraded to `private`.

uv's older, pip-compatible flat `index-url`/`extra-index-url` keys are
now recognized too (fixed in v0.1.87). Before uv grew the
`[[tool.uv.index]]` array, it modeled a blanket default/extra index the
same flat way pip does — a single `index-url = "..."` string and an
`extra-index-url = [...]` list, settable at the top level of either a
standalone `uv.toml` or a pyproject.toml's `[tool.uv]` table. uv's own
settings reference marks both "Deprecated: use `index` instead" —
deprecated, not removed. Confirmed live with real uv 0.12.19, three
ways: a pyproject.toml's `[tool.uv]` table with only `index-url` set
and no `[[tool.uv.index]]` entry at all; the same table with only
`extra-index-url` set; and a standalone `uv.toml` with only
`index-url` set and zero `[tool.uv]` section in pyproject.toml — all
three made a real `uv lock -v` genuinely issue a GET against the
configured address for a dependency name that 404s on the real public
PyPI JSON API, instead of leaving it unresolved. Before this fix,
either deprecated key (still a real, current pattern — uv's own docs
keep documenting it as a working pip-compatible shorthand, e.g. for
pinning a PyTorch CPU wheel index) left every genuinely-resolvable
private/extra dependency reported as a plain `not_found` hallucination
instead of downgraded to `private`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Related tools

Other no-signup CLIs from the same org:

- **[modslop](https://github.com/experimental-gains/modslop)** — the same hallucinated/slopsquatted-name check for Go module paths in `go.mod`
- **[hfaudit](https://github.com/experimental-gains/hfaudit)** — the same check for Hugging Face Hub model/dataset IDs
- **[goproxycheck](https://github.com/experimental-gains/goproxycheck)** — diagnoses why a Go module version won't fetch via the public proxy/sumdb
- **[goprivaudit](https://github.com/experimental-gains/goprivaudit)** — audits `GOPRIVATE`/`GONOSUMDB` config for private-module sumdb leaks

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
