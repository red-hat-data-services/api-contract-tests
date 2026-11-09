#!/usr/bin/env python3
"""release-baseline: find the commit an RHOAI release was based on, for use as a baseline SHA.

A provider's baseline must be a full SHA that exists in the repository the check
runs in. RHOAI releases are built from the downstream repository
(red-hat-data-services/<name>, branch rhoai-<release>), and those commits do not
exist in the upstream opendatahub-io repository, so the SHA of the release branch
cannot be used directly. The newest commit that both histories share (the merge
base of the release commit and the upstream default branch) does exist in both and
is the baseline to use.

    release_baseline.py resolve --release 3.6 --repo opendatahub-io/odh-dashboard
    release_baseline.py resolve --release 3.6 --repo opendatahub-io/kserve \\
        --image quay.io/opendatahub/kserve-controller:odh-stable
    release_baseline.py lookup  --release 3.6 --repo opendatahub-io/odh-dashboard
    release_baseline.py generate --release 3.6 --repos-file data/release-baselines/repos.txt \\
        --output data/release-baselines/3.6.json

`resolve` asks the GitHub API (set GITHUB_TOKEN or GH_TOKEN, or log in with `gh`; the
anonymous rate limit is enough for one repository). `lookup` reads the checked-in
result of `generate` and needs no network. Exit status: 0 resolved, 2 not resolved
(the message says why; fall back to the SHA that the default branch resolves to).

The release commit comes from the downstream release branch. With --image it comes
from the source-commit label of a release image instead, which is what actually
shipped. The label is read from vcs-ref, git.commit or io.openshift.build.commit.id.
org.opencontainers.image.revision is deliberately ignored: base images pass their own
value through, so it can name a commit that is not in the repository.

This uses the standard library only.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API = "https://api.github.com"
DOWNSTREAM_ORG = "red-hat-data-services"
RELEASE_BRANCH = "rhoai-{release}"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
RELEASE = re.compile(r"[0-9]+\.[0-9]+")
# Downstream names of upstream repositories that were renamed downstream.
DOWNSTREAM_NAMES = {
    "opendatahub-operator": "rhods-operator",
    "opendatahub-db-operator": "rhai-db-operator",
}
LABEL_SHA = ("vcs-ref", "git.commit", "io.openshift.build.commit.id")
LABEL_URL = ("vcs-url", "git.url", "io.openshift.build.source-location")
MANIFEST_TYPES = ",".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))
DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data" / "release-baselines"

Response = tuple[int, Mapping[str, str], bytes]
Fetch = Callable[[str, Mapping[str, str]], Response]


class ResolveError(Exception):
    """The release baseline could not be determined; the message says why."""


def http_fetch(url: str, headers: Mapping[str, str]) -> Response:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()
    except urllib.error.URLError as error:
        raise ResolveError(f"cannot reach {url}: {error.reason}") from error


def github_token(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if env.get(name):
            return env[name]
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


class GitHub:
    """The few GitHub REST calls needed to relate a downstream commit to its upstream."""

    def __init__(self, fetch: Fetch, token: str | None = None) -> None:
        self.fetch = fetch
        self.headers = {"Accept": "application/vnd.github+json", "User-Agent": "api-contract-tests"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str) -> Any:
        status, _, body = self.fetch(f"{API}{path}", self.headers)
        if status == 404:
            raise ResolveError(f"not found: {path}")
        if status in (401, 403):
            raise ResolveError(f"GitHub API refused {path} ({status}); set GITHUB_TOKEN or log in with gh")
        if status >= 400:
            raise ResolveError(f"GitHub API returned {status} for {path}")
        return json.loads(body)

    def default_branch(self, repo: str) -> str:
        return str(self.get(f"/repos/{repo}")["default_branch"])

    def branch_sha(self, repo: str, branch: str) -> str:
        return str(self.get(f"/repos/{repo}/branches/{urllib.parse.quote(branch, safe='/')}")["commit"]["sha"])

    def compare(self, repo: str, base: str, head: str) -> Mapping[str, Any]:
        base_ref = urllib.parse.quote(base, safe="/")
        result = self.get(f"/repos/{repo}/compare/{base_ref}...{head}")
        return result


# Image labels

@dataclass(frozen=True)
class ImageRef:
    host: str
    repository: str
    reference: str


def parse_image_ref(ref: str) -> ImageRef:
    name, _, digest = ref.partition("@")
    reference = digest
    if not digest:
        head, _, last = name.rpartition("/")
        if ":" in last:
            last, _, reference = last.partition(":")
            name = f"{head}/{last}" if head else last
        else:
            reference = "latest"
    host, _, repository = name.partition("/")
    if not repository or ("." not in host and ":" not in host and host != "localhost"):
        raise ResolveError(f"image reference needs a registry host, for example quay.io/org/name:tag: {ref}")
    return ImageRef(host, repository, reference)


def registry_auth(host: str, files: Sequence[Path]) -> str | None:
    """The stored basic-auth value for host, or None. The value is never printed."""
    for path in files:
        try:
            auths = json.loads(path.read_text()).get("auths", {})
        except (OSError, ValueError):
            continue
        entry = auths.get(host)
        if isinstance(entry, dict) and entry.get("auth"):
            return str(entry["auth"])
    return None


def default_auth_files(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    files = [Path(env["REGISTRY_AUTH_FILE"])] if env.get("REGISTRY_AUTH_FILE") else []
    home = Path(env.get("HOME", "~")).expanduser()
    return files + [home / ".config" / "containers" / "auth.json", home / ".docker" / "config.json"]


def read_image_labels(ref: str, fetch: Fetch, auth_files: Sequence[Path] = ()) -> dict[str, str]:
    """Labels of the image's config blob (first linux/amd64 manifest of an index)."""
    image = parse_image_ref(ref)
    base = f"https://{image.host}/v2/{image.repository}"
    headers: dict[str, str] = {"Accept": MANIFEST_TYPES}

    def get(url: str, accept: bool = True) -> Any:
        sent = dict(headers) if accept else {k: v for k, v in headers.items() if k != "Accept"}
        status, response_headers, body = fetch(url, sent)
        if status == 401 and "Authorization" not in headers:
            headers["Authorization"] = "Bearer " + bearer_token(image, response_headers.get("www-authenticate", ""), fetch, auth_files)
            status, response_headers, body = fetch(url, {k: v for k, v in headers.items() if accept or k != "Accept"})
        if status >= 400:
            raise ResolveError(f"cannot read {ref}: registry returned {status} for {url}")
        return json.loads(body)

    manifest = get(f"{base}/manifests/{image.reference}")
    if "manifests" in manifest:
        entries = manifest["manifests"]
        chosen = next((e for e in entries if e.get("platform", {}).get("architecture") == "amd64"), entries[0])
        manifest = get(f"{base}/manifests/{chosen['digest']}")
    config = get(f"{base}/blobs/{manifest['config']['digest']}", accept=False)
    labels = (config.get("config") or {}).get("Labels") or {}
    return {str(k): str(v) for k, v in labels.items()}


def bearer_token(image: ImageRef, challenge: str, fetch: Fetch, auth_files: Sequence[Path]) -> str:
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if "realm" not in fields:
        raise ResolveError(f"registry {image.host} requires authentication it did not describe")
    query = {"scope": f"repository:{image.repository}:pull"}
    if "service" in fields:
        query["service"] = fields["service"]
    headers: dict[str, str] = {}
    stored = registry_auth(image.host, auth_files)
    if stored:
        headers["Authorization"] = "Basic " + stored
    status, _, body = fetch(f"{fields['realm']}?{urllib.parse.urlencode(query)}", headers)
    if status != 200:
        raise ResolveError(f"registry {image.host} refused a token for {image.repository} ({status}); "
                           "the stored credentials may not cover this repository")
    data = json.loads(body)
    token = data.get("token") or data.get("access_token")
    if not token:
        raise ResolveError(f"registry {image.host} returned no token")
    return str(token)


def label_commit(labels: Mapping[str, str], repos: Sequence[str]) -> tuple[str, list[str]]:
    """The source commit named by the labels, and warnings about its provenance."""
    sha = next((labels[k] for k in LABEL_SHA if FULL_SHA.fullmatch(labels.get(k, ""))), None)
    if sha is None:
        raise ResolveError("the image has no source-commit label (looked for "
                           f"{', '.join(LABEL_SHA)}); use the release branch instead")
    url = next((labels[k] for k in LABEL_URL if labels.get(k)), "")
    warnings: list[str] = []
    wanted = [f"github.com/{repo}".lower() for repo in repos]
    if url and not any(url.lower().rstrip("/").removesuffix(".git").endswith(w) for w in wanted):
        warnings.append(f"the image's source URL is {url}, not {' or '.join(repos)}")
    return sha, warnings


# Resolution

@dataclass(frozen=True)
class Resolution:
    repo: str
    downstream_repo: str
    release: str
    release_source: str
    release_ref: str
    release_sha: str
    baseline_sha: str
    default_branch: str
    relation: str
    release_only_commits: int
    repo_only_commits: int
    warnings: tuple[str, ...] = ()


def downstream_repo_for(repo: str) -> str:
    name = repo.split("/", 1)[1]
    return f"{DOWNSTREAM_ORG}/{DOWNSTREAM_NAMES.get(name, name)}"


def resolve(gh: GitHub, repo: str, release: str, downstream: str | None = None,
            image: str | None = None, image_labels: Mapping[str, str] | None = None) -> Resolution:
    if not RELEASE.fullmatch(release):
        raise ResolveError(f"release must look like 3.6, got {release!r}")
    if "/" not in repo:
        raise ResolveError(f"repository must be ORG/NAME, got {repo!r}")
    downstream = downstream or downstream_repo_for(repo)
    warnings: list[str] = []
    if image_labels is not None:
        release_sha, warnings = label_commit(image_labels, [downstream, repo])
        source, release_ref = "image-label", image or "image"
    else:
        release_ref = RELEASE_BRANCH.format(release=release)
        try:
            release_sha = gh.branch_sha(downstream, release_ref)
        except ResolveError as error:
            raise ResolveError(f"{downstream} has no branch {release_ref} ({error})") from error
        source = "branch"
    default = gh.default_branch(repo)
    if repo == downstream:
        # The release commit lives in this repository, so it is the baseline itself.
        return Resolution(repo, downstream, release, source, release_ref, release_sha, release_sha,
                          default, "same-repository", 0, 0, tuple(warnings))
    try:
        comparison = gh.compare(repo, default, release_sha)
    except ResolveError as error:
        raise ResolveError(f"{repo} and {downstream} share no history that GitHub can compare ({error}); "
                           "use the SHA that the default branch resolves to") from error
    base = comparison.get("merge_base_commit", {}).get("sha", "")
    if not FULL_SHA.fullmatch(base):
        raise ResolveError(f"{repo} and {downstream} have no common commit; use the SHA that the default branch resolves to")
    return Resolution(repo, downstream, release, source, release_ref, release_sha, base, default,
                      str(comparison["status"]), int(comparison["ahead_by"]), int(comparison["behind_by"]),
                      tuple(warnings))


def describe(result: Resolution) -> str:
    lines = [
        f"baseline  {result.baseline_sha}",
        f"release   {result.release_sha}  ({result.release_source}: {result.release_ref} in {result.downstream_repo})",
    ]
    if result.relation == "same-repository":
        lines.append("note      the release commit is in this repository, so it is used directly")
    else:
        lines.append(f"relation  {result.release_only_commits} commits in the release are not in {result.repo}@{result.default_branch}; "
                     f"{result.repo_only_commits} commits in {result.default_branch} are not in the release")
        lines.append("note      the release commit itself is downstream-only; the baseline is the newest commit both repositories share")
    lines += [f"warning   {w}" for w in result.warnings]
    return "\n".join(lines)


# Checked-in data

def read_pairs(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        pairs.append((parts[0], parts[1] if len(parts) > 1 else downstream_repo_for(parts[0])))
    return pairs


def generate(gh: GitHub, release: str, pairs: Sequence[tuple[str, str]], now: datetime | None = None) -> dict[str, Any]:
    def one(pair: tuple[str, str]) -> tuple[str, Resolution | str]:
        try:
            return pair[0], resolve(gh, pair[0], release, downstream=pair[1])
        except ResolveError as error:
            return pair[0], str(error)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(one, pairs))
    repos: dict[str, Any] = {}
    unresolved: dict[str, str] = {}
    for repo, outcome in sorted(outcomes):
        if isinstance(outcome, str):
            unresolved[repo] = outcome
        else:
            data = asdict(outcome)
            repos[repo] = {k: data[k] for k in (
                "baseline_sha", "downstream_repo", "release_ref", "release_sha", "default_branch",
                "relation", "release_only_commits", "repo_only_commits")}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"release": release, "generated": stamp,
            "method": "baseline = merge base of the downstream release branch and the upstream default branch",
            "repos": repos, "unresolved": dict(sorted(unresolved.items()))}


def lookup(data: Mapping[str, Any], repo: str) -> Mapping[str, Any]:
    entry = data.get("repos", {}).get(repo)
    if entry is None:
        reason = data.get("unresolved", {}).get(repo)
        raise ResolveError(f"{repo} has no recorded baseline for release {data.get('release')}"
                           + (f": {reason}" if reason else "") + "; use the SHA that the default branch resolves to")
    return entry


# Command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_baseline.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("resolve", "lookup"):
        p = sub.add_parser(name)
        p.add_argument("--release", required=True, help="release, for example 3.6")
        p.add_argument("--repo", required=True, help="repository the baseline is for, ORG/NAME")
        p.add_argument("--format", choices=("text", "sha", "json"), default="text")
        if name == "resolve":
            p.add_argument("--downstream", help=f"downstream repository (default {DOWNSTREAM_ORG}/<name>)")
            p.add_argument("--image", help="release image whose source-commit label names the release commit")
        else:
            p.add_argument("--data", type=Path, help="data file (default data/release-baselines/<release>.json)")
    p = sub.add_parser("generate")
    p.add_argument("--release", required=True)
    p.add_argument("--repos-file", type=Path, required=True, help="lines of: UPSTREAM [DOWNSTREAM]")
    p.add_argument("--output", type=Path, required=True)
    return parser


def run(argv: Sequence[str], fetch: Fetch = http_fetch, token: str | None = None,
        auth_files: Sequence[Path] | None = None, out: Any = None, err: Any = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lookup":
            path = args.data or DEFAULT_DATA / f"{args.release}.json"
            try:
                data = json.loads(path.read_text())
            except OSError as error:
                raise ResolveError(f"cannot read {path}: {error.strerror}") from error
            if data.get("release") != args.release:
                raise ResolveError(f"{path} is for release {data.get('release')}, not {args.release}")
            entry = lookup(data, args.repo)
            if args.format == "sha":
                print(entry["baseline_sha"], file=out)
            elif args.format == "json":
                print(json.dumps({"repo": args.repo, "generated": data.get("generated"), **entry}, indent=2), file=out)
            else:
                print(f"baseline  {entry['baseline_sha']}\nrelease   {entry['release_sha']}  "
                      f"({entry['release_ref']} in {entry['downstream_repo']})\ngenerated {data.get('generated')}", file=out)
            return 0
        gh = GitHub(fetch, token if token is not None else github_token())
        if args.command == "generate":
            result = generate(gh, args.release, read_pairs(args.repos_file))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            print(f"{len(result['repos'])} resolved, {len(result['unresolved'])} unresolved -> {args.output}", file=out)
            return 0
        labels = None
        if args.image:
            files = default_auth_files() if auth_files is None else auth_files
            labels = read_image_labels(args.image, fetch, files)
        resolution = resolve(gh, args.repo, args.release, args.downstream, args.image, labels)
        if args.format == "sha":
            print(resolution.baseline_sha, file=out)
        elif args.format == "json":
            print(json.dumps(asdict(resolution), indent=2), file=out)
        else:
            print(describe(resolution), file=out)
        return 0
    except ResolveError as error:
        print(f"release-baseline: {error}", file=err)
        return 2


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
