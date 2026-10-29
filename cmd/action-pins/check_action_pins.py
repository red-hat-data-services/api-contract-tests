#!/usr/bin/env python3
"""Check that every action a workflow uses is pinned to the commit its version comment names.

Workflows in this repository pin actions by full commit SHA, with the release as a
trailing comment:

    uses: actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c # v6.0.0

A mistyped SHA is easy to miss in review, and GitHub only rejects it when a job
runs. The provider workflows run only when a provider calls them, so a bad pin can
go unnoticed until then. This check resolves each comment's tag with
``git ls-remote`` and fails unless the tag points at the pinned SHA, which also
catches a SHA that exists but belongs to a different release.

    python3 cmd/action-pins/check_action_pins.py [workflow-dir-or-file ...]

Exit codes: 0 when every pin matches, 1 when a pin is wrong, 2 when a tag could not
be resolved (for example, no network).
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<target>[^\s#]+)(?:\s+#\s*(?P<comment>\S+))?")
FULL_SHA = re.compile(r"[0-9a-f]{40}")

# (owner/repo, tag) -> commit SHA the tag points at, or None when the tag does not exist.
Resolver = Callable[[str, str], str | None]


class ResolveError(Exception):
    """A tag could not be looked up, so the pin could not be checked."""


def tag_commit(ls_remote_output: str, tag: str) -> str | None:
    """The commit a tag points at, from ``git ls-remote`` output.

    An annotated tag is listed twice: the tag object, and the commit it points at
    with a ``^{}`` suffix. The commit is what a workflow pins.
    """
    direct = peeled = None
    for line in ls_remote_output.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            direct = sha
    return peeled or direct


def ls_remote(repository: str, tag: str) -> str | None:
    url = f"https://github.com/{repository}"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ResolveError(f"Cannot run git ls-remote for {url}: {error}") from error
    if result.returncode:
        raise ResolveError(f"git ls-remote {url} failed: {result.stderr.strip()}")
    return tag_commit(result.stdout, tag)


def workflow_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix in (".yml", ".yaml")))
        else:
            files.append(path)
    return files


def check_file(path: Path, resolve: Resolver) -> list[str]:
    problems = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = USES.match(line)
        if match is None:
            continue
        target, comment = match["target"], match["comment"]
        # Local actions and reusable workflows come from this checkout; Docker images
        # are not Git refs.
        if target.startswith(("./", "docker://")):
            continue
        where = f"{path}:{number}"
        action, _, ref = target.rpartition("@")
        if not action:
            problems.append(f"{where}: {target} has no @ref")
            continue
        if FULL_SHA.fullmatch(ref) is None:
            problems.append(f"{where}: {action} is pinned to {ref!r}, not a full commit SHA")
            continue
        if not comment:
            problems.append(f"{where}: {action}@{ref} has no '# <tag>' comment to check the pin against")
            continue
        repository = "/".join(action.split("/")[:2])
        tagged = resolve(repository, comment)
        if tagged is None:
            problems.append(f"{where}: {action}: tag {comment} does not exist in {repository}")
        elif tagged != ref:
            problems.append(f"{where}: {action} pins {ref}, but {comment} is {tagged}")
    return problems


def cached(resolve: Resolver) -> Resolver:
    seen: dict[tuple[str, str], str | None] = {}

    def lookup(repository: str, tag: str) -> str | None:
        if (repository, tag) not in seen:
            seen[(repository, tag)] = resolve(repository, tag)
        return seen[(repository, tag)]

    return lookup


def main(argv: Sequence[str] | None = None, resolve: Resolver = ls_remote) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path(".github/workflows")],
                        help="Workflow files or directories (default .github/workflows)")
    args = parser.parse_args(argv)

    lookup = cached(resolve)
    problems = []
    try:
        for path in workflow_files(args.paths):
            problems.extend(check_file(path, lookup))
    except (OSError, ResolveError) as error:
        print(str(error), file=sys.stderr)
        return 2
    for text in problems:
        print(text)
    if problems:
        return 1
    print("Every action pin matches its version comment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
