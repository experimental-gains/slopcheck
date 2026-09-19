from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .parsers import Dependency, find_manifests, parse_manifest
from .registries import CHECKERS, LookupResult

_SEVERITY_ORDER = {"not_found": 0, "recent": 1, "error": 2, "ok": 3}


def _check_one(dep: Dependency) -> tuple[Dependency, LookupResult]:
    checker = CHECKERS[dep.ecosystem]
    return dep, checker(dep.name)


def scan(paths: list[Path], max_workers: int = 16) -> list[tuple[Dependency, LookupResult]]:
    deps: list[Dependency] = []
    for path in paths:
        deps.extend(parse_manifest(path))

    # De-dupe same name+ecosystem across files, keep first source for reporting.
    seen: dict[tuple[str, str], Dependency] = {}
    for dep in deps:
        key = (dep.ecosystem, dep.name.lower())
        seen.setdefault(key, dep)
    unique_deps = list(seen.values())

    results: list[tuple[Dependency, LookupResult]] = []
    if not unique_deps:
        return results
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for dep, result in pool.map(_check_one, unique_deps):
            results.append((dep, result))

    results.sort(key=lambda pair: _SEVERITY_ORDER[pair[1].status])
    return results


def _print_report(results: list[tuple[Dependency, LookupResult]]) -> None:
    labels = {
        "not_found": "\033[31mNOT FOUND\033[0m",
        "recent": "\033[33mRECENT   \033[0m",
        "error": "\033[33mERROR    \033[0m",
        "ok": "\033[32mok       \033[0m",
    }
    for dep, result in results:
        if result.status == "ok":
            continue
        detail = f" ({result.detail})" if result.detail else ""
        print(f"  {labels[result.status]} {dep.ecosystem:5s} {dep.name}{detail} [{dep.source}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slopcheck",
        description=(
            "Check whether every dependency in your manifests actually exists in its "
            "registry, and flag suspiciously new packages. Catches hallucinated "
            "('slopsquatted') package names before you install them."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Manifest files to check, or directories to search "
        "(requirements.txt, pyproject.toml, package.json). Defaults to the current directory.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    parser.add_argument(
        "--fail-on",
        choices=["not_found", "recent", "never"],
        default="not_found",
        help="Minimum severity that causes a non-zero exit code (default: not_found).",
    )
    args = parser.parse_args(argv)

    manifests: list[Path] = []
    targets = args.paths or [Path(".")]
    for target in targets:
        if target.is_dir():
            manifests.extend(find_manifests(target))
        elif target.is_file():
            manifests.append(target)
        else:
            print(f"slopcheck: no such file or directory: {target}", file=sys.stderr)
            return 2

    if not manifests:
        print("slopcheck: no requirements.txt, pyproject.toml, or package.json found", file=sys.stderr)
        return 2

    results = scan(manifests)

    if args.json:
        payload = [
            {
                "name": dep.name,
                "ecosystem": dep.ecosystem,
                "source": dep.source,
                "status": result.status,
                "detail": result.detail,
            }
            for dep, result in results
        ]
        print(json.dumps(payload, indent=2))
    else:
        flagged = [pair for pair in results if pair[1].status != "ok"]
        if not flagged:
            print(f"slopcheck: {len(results)} dependencies checked, all clean")
        else:
            print(f"slopcheck: {len(flagged)} of {len(results)} dependencies flagged")
            _print_report(results)

    if args.fail_on == "never":
        return 0
    threshold = _SEVERITY_ORDER[args.fail_on]
    if any(_SEVERITY_ORDER[result.status] <= threshold for _, result in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
