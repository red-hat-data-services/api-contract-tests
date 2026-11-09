"""Tests for the release baseline resolver (cmd/release-baseline/release_baseline.py).

GitHub and the registry are faked in memory, so these tests need no network and no
credentials.
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "cmd" / "release-baseline" / "release_baseline.py"

spec = importlib.util.spec_from_file_location("release_baseline", MODULE)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)


def sha(char):
    return char * 40


RELEASE_SHA = sha("a")   # downstream-only commit at the head of rhoai-3.6
SHARED = sha("b")        # newest commit both histories share
UPSTREAM_HEAD = sha("c")


class FakeGitHub:
    """Answers the three REST calls the resolver makes, from a table of repositories."""

    def __init__(self, repos):
        self.repos = repos
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        path = url.removeprefix(rb.API)
        parts = path.strip("/").split("/")
        repo = "/".join(parts[1:3])
        if repo not in self.repos:
            return 404, {}, b"{}"
        data = self.repos[repo]
        if len(parts) == 3:
            return 200, {}, json.dumps({"default_branch": data["default"]}).encode()
        if parts[3] == "branches":
            branch = "/".join(parts[4:])
            if branch not in data["branches"]:
                return 404, {}, b"{}"
            return 200, {}, json.dumps({"commit": {"sha": data["branches"][branch]}}).encode()
        if parts[3] == "compare":
            head = parts[4].split("...")[1]
            comparison = data["compare"].get(head)
            if comparison is None:
                return 404, {}, b"{}"
            return 200, {}, json.dumps(comparison).encode()
        return 500, {}, b"{}"


def diverged(mb=SHARED, ahead=12, behind=3):
    return {"status": "diverged", "ahead_by": ahead, "behind_by": behind, "merge_base_commit": {"sha": mb}}


def world(compare=None, branches=None):
    return {
        "opendatahub-io/kserve": {"default": "master", "branches": {"master": UPSTREAM_HEAD},
                                  "compare": {RELEASE_SHA: compare or diverged()}},
        "red-hat-data-services/kserve": {"default": "main", "branches": branches or {"main": sha("d"), "rhoai-3.6": RELEASE_SHA},
                                         "compare": {}},
    }


def resolve(repos, repo="opendatahub-io/kserve", **kwargs):
    return rb.resolve(rb.GitHub(FakeGitHub(repos)), repo, "3.6", **kwargs)


# resolve: the release branch

def test_baseline_is_the_merge_base_not_the_downstream_head():
    result = resolve(world())
    assert result.baseline_sha == SHARED
    assert result.release_sha == RELEASE_SHA
    assert result.release_source == "branch"
    assert result.release_ref == "rhoai-3.6"
    assert result.downstream_repo == "red-hat-data-services/kserve"
    assert (result.relation, result.release_only_commits, result.repo_only_commits) == ("diverged", 12, 3)


def test_release_that_contains_all_of_upstream_uses_the_upstream_head():
    result = resolve(world(compare={"status": "ahead", "ahead_by": 40, "behind_by": 0,
                                    "merge_base_commit": {"sha": UPSTREAM_HEAD}}))
    assert result.baseline_sha == UPSTREAM_HEAD
    assert result.relation == "ahead"


def test_release_commit_already_in_upstream_is_its_own_baseline():
    result = resolve(world(compare={"status": "behind", "ahead_by": 0, "behind_by": 7,
                                    "merge_base_commit": {"sha": RELEASE_SHA}}))
    assert result.baseline_sha == RELEASE_SHA


@pytest.mark.parametrize("upstream,downstream", [
    ("opendatahub-io/opendatahub-operator", "red-hat-data-services/rhods-operator"),
    ("opendatahub-io/opendatahub-db-operator", "red-hat-data-services/rhai-db-operator"),
    ("opendatahub-io/odh-dashboard", "red-hat-data-services/odh-dashboard"),
])
def test_downstream_names_follow_the_known_renames(upstream, downstream):
    assert rb.downstream_repo_for(upstream) == downstream


def test_explicit_downstream_overrides_the_name_rule():
    repos = world()
    repos["red-hat-data-services/other"] = repos.pop("red-hat-data-services/kserve")
    assert resolve(repos, downstream="red-hat-data-services/other").downstream_repo == "red-hat-data-services/other"


def test_repository_that_is_its_own_downstream_uses_the_release_head_directly():
    repos = {"red-hat-data-services/kserve": world()["red-hat-data-services/kserve"]}
    result = resolve(repos, repo="red-hat-data-services/kserve")
    assert result.baseline_sha == RELEASE_SHA
    assert result.relation == "same-repository"


def test_missing_release_branch_says_so_and_names_the_fallback():
    with pytest.raises(rb.ResolveError, match="no branch rhoai-3.6"):
        resolve(world(branches={"main": sha("d")}))


def test_repositories_without_shared_history_are_reported_not_guessed():
    repos = world()
    repos["opendatahub-io/kserve"]["compare"] = {}
    with pytest.raises(rb.ResolveError, match="share no history.*default branch resolves to"):
        resolve(repos)


def test_missing_merge_base_is_an_error():
    with pytest.raises(rb.ResolveError, match="no common commit"):
        resolve(world(compare={"status": "diverged", "ahead_by": 1, "behind_by": 1, "merge_base_commit": {}}))


@pytest.mark.parametrize("release", ["3", "3.6.1", "v3.6", "latest", ""])
def test_release_must_look_like_major_dot_minor(release):
    with pytest.raises(rb.ResolveError, match="release must look like"):
        rb.resolve(rb.GitHub(FakeGitHub(world())), "opendatahub-io/kserve", release)


def test_repository_needs_an_organization():
    with pytest.raises(rb.ResolveError, match="ORG/NAME"):
        rb.resolve(rb.GitHub(FakeGitHub(world())), "kserve", "3.6")


# GitHub client

def test_authorization_header_is_sent_only_when_a_token_is_given():
    seen = []

    def fetch(url, headers):
        seen.append(dict(headers))
        return 200, {}, b'{"default_branch": "main"}'

    rb.GitHub(fetch, "t0ken").default_branch("o/r")
    rb.GitHub(fetch).default_branch("o/r")
    assert seen[0]["Authorization"] == "Bearer t0ken"
    assert "Authorization" not in seen[1]


@pytest.mark.parametrize("status,message", [(403, "set GITHUB_TOKEN"), (401, "set GITHUB_TOKEN"), (500, "returned 500")])
def test_api_failures_have_actionable_messages(status, message):
    gh = rb.GitHub(lambda url, headers: (status, {}, b"{}"))
    with pytest.raises(rb.ResolveError, match=message):
        gh.default_branch("o/r")


def test_token_comes_from_the_environment_first():
    assert rb.github_token({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}) == "a"
    assert rb.github_token({"GH_TOKEN": "b"}) == "b"


# image labels

LABELS = {"vcs-ref": RELEASE_SHA, "vcs-url": "https://github.com/red-hat-data-services/kserve",
          "org.opencontainers.image.revision": sha("e")}


def test_label_commit_prefers_vcs_ref_and_ignores_the_oci_revision():
    found, warnings = rb.label_commit(LABELS, ["red-hat-data-services/kserve", "opendatahub-io/kserve"])
    assert found == RELEASE_SHA
    assert warnings == []


@pytest.mark.parametrize("labels", [
    {"git.commit": RELEASE_SHA},
    {"io.openshift.build.commit.id": RELEASE_SHA},
])
def test_label_commit_falls_back_to_other_known_labels(labels):
    assert rb.label_commit(labels, ["o/r"])[0] == RELEASE_SHA


@pytest.mark.parametrize("labels", [
    {},
    {"vcs-ref": "main"},
    {"vcs-ref": "abc123"},
    {"org.opencontainers.image.revision": RELEASE_SHA},
])
def test_label_commit_rejects_missing_or_abbreviated_commits(labels):
    with pytest.raises(rb.ResolveError, match="no source-commit label"):
        rb.label_commit(labels, ["o/r"])


def test_label_pointing_at_another_repository_warns():
    _, warnings = rb.label_commit({"vcs-ref": RELEASE_SHA, "vcs-url": "https://github.com/someone/else"}, ["o/r"])
    assert warnings and "someone/else" in warnings[0]


def test_url_with_dot_git_suffix_matches():
    _, warnings = rb.label_commit({"vcs-ref": RELEASE_SHA, "vcs-url": "https://github.com/o/r.git"}, ["o/r"])
    assert warnings == []


def test_image_label_wins_over_the_branch_head():
    labels = {"vcs-ref": sha("f"), "vcs-url": "https://github.com/red-hat-data-services/kserve"}
    repos = world()
    repos["opendatahub-io/kserve"]["compare"] = {sha("f"): diverged(mb=sha("9"))}
    result = resolve(repos, image="quay.io/rhoai/kserve:rhoai-3.6", image_labels=labels)
    assert result.release_source == "image-label"
    assert result.release_sha == sha("f")
    assert result.baseline_sha == sha("9")


# registry access

class FakeRegistry:
    def __init__(self, labels, secured=False, index=False):
        self.labels, self.secured, self.index = labels, secured, index
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        if url.startswith("https://auth.example/token"):
            if "Authorization" not in headers:
                return 401, {}, b"{}"
            return 200, {}, json.dumps({"token": "T"}).encode()
        if self.secured and headers.get("Authorization") != "Bearer T":
            challenge = 'Bearer realm="https://auth.example/token",service="reg"'
            return 401, {"www-authenticate": challenge}, b"{}"
        if "/manifests/latest" in url or "/manifests/tag" in url:
            if self.index:
                return 200, {}, json.dumps({"manifests": [
                    {"digest": "sha256:arm", "platform": {"architecture": "arm64"}},
                    {"digest": "sha256:amd", "platform": {"architecture": "amd64"}}]}).encode()
            return 200, {}, json.dumps({"config": {"digest": "sha256:cfg"}}).encode()
        if "/manifests/sha256:amd" in url:
            return 200, {}, json.dumps({"config": {"digest": "sha256:cfg"}}).encode()
        if "/blobs/sha256:cfg" in url:
            return 200, {}, json.dumps({"config": {"Labels": self.labels}}).encode()
        return 404, {}, b"{}"


@pytest.mark.parametrize("ref,expected", [
    ("quay.io/org/name:tag", ("quay.io", "org/name", "tag")),
    ("quay.io/org/name", ("quay.io", "org/name", "latest")),
    ("quay.io/org/name@sha256:abc", ("quay.io", "org/name", "sha256:abc")),
    ("registry.redhat.io/rhoai/odh-dashboard-rhel9:v3.6", ("registry.redhat.io", "rhoai/odh-dashboard-rhel9", "v3.6")),
    ("localhost:5000/team/img:1", ("localhost:5000", "team/img", "1")),
])
def test_image_references_are_split_into_host_repository_and_reference(ref, expected):
    parsed = rb.parse_image_ref(ref)
    assert (parsed.host, parsed.repository, parsed.reference) == expected


@pytest.mark.parametrize("ref", ["ubuntu", "org/name:tag"])
def test_image_references_need_a_registry_host(ref):
    with pytest.raises(rb.ResolveError, match="registry host"):
        rb.parse_image_ref(ref)


def test_labels_are_read_from_the_config_blob():
    registry = FakeRegistry({"vcs-ref": RELEASE_SHA})
    assert rb.read_image_labels("reg.example/org/name:tag", registry) == {"vcs-ref": RELEASE_SHA}


def test_image_index_selects_the_amd64_manifest():
    registry = FakeRegistry({"vcs-ref": RELEASE_SHA}, index=True)
    rb.read_image_labels("reg.example/org/name:tag", registry)
    assert any("/manifests/sha256:amd" in url for url, _ in registry.requests)
    assert not any("sha256:arm" in url for url, _ in registry.requests)


def test_blob_request_does_not_ask_for_a_manifest_media_type():
    registry = FakeRegistry({}, index=False)
    rb.read_image_labels("reg.example/org/name:tag", registry)
    blob = next(h for url, h in registry.requests if "/blobs/" in url)
    assert "Accept" not in blob


def test_token_flow_uses_stored_credentials_without_exposing_them(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auths": {"reg.example": {"auth": "c2VjcmV0"}}}))
    registry = FakeRegistry({"vcs-ref": RELEASE_SHA}, secured=True)
    assert rb.read_image_labels("reg.example/org/name:tag", registry, [auth]) == {"vcs-ref": RELEASE_SHA}
    token_request = next((u, h) for u, h in registry.requests if u.startswith("https://auth.example/token"))
    assert token_request[1]["Authorization"] == "Basic c2VjcmV0"
    assert "scope=repository%3Aorg%2Fname%3Apull" in token_request[0]


def test_refused_token_explains_the_credentials_may_not_cover_the_repository():
    registry = FakeRegistry({}, secured=True)
    with pytest.raises(rb.ResolveError, match="stored credentials may not cover"):
        rb.read_image_labels("reg.example/org/name:tag", registry, [])


def test_registry_auth_ignores_missing_and_malformed_files(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert rb.registry_auth("reg.example", [tmp_path / "absent.json", bad]) is None


# generate and lookup

def pairs_world():
    repos = world()
    repos["opendatahub-io/modelexpress"] = {"default": "main", "branches": {"main": sha("1")}, "compare": {}}
    repos["red-hat-data-services/modelexpress"] = {"default": "main", "branches": {"main": sha("2"), "rhoai-3.6": sha("3")}, "compare": {}}
    return repos


def test_generate_records_resolved_and_unresolved_repositories():
    from datetime import datetime, timezone
    gh = rb.GitHub(FakeGitHub(pairs_world()))
    data = rb.generate(gh, "3.6", [("opendatahub-io/kserve", "red-hat-data-services/kserve"),
                                   ("opendatahub-io/modelexpress", "red-hat-data-services/modelexpress")],
                       now=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc))
    assert data["release"] == "3.6" and data["generated"] == "2026-11-09T12:00:00Z"
    assert data["repos"]["opendatahub-io/kserve"]["baseline_sha"] == SHARED
    assert "opendatahub-io/modelexpress" in data["unresolved"]
    assert "share no history" in data["unresolved"]["opendatahub-io/modelexpress"]


def test_read_pairs_skips_comments_and_derives_the_downstream_name(tmp_path):
    path = tmp_path / "repos.txt"
    path.write_text("# header\nopendatahub-io/kserve\n\nopendatahub-io/opendatahub-operator  # renamed\n"
                    "opendatahub-io/x red-hat-data-services/y\n")
    assert rb.read_pairs(path) == [
        ("opendatahub-io/kserve", "red-hat-data-services/kserve"),
        ("opendatahub-io/opendatahub-operator", "red-hat-data-services/rhods-operator"),
        ("opendatahub-io/x", "red-hat-data-services/y"),
    ]


def test_lookup_explains_unresolved_repositories():
    data = {"release": "3.6", "repos": {}, "unresolved": {"o/r": "no history"}}
    with pytest.raises(rb.ResolveError, match="no history.*default branch resolves to"):
        rb.lookup(data, "o/r")
    with pytest.raises(rb.ResolveError, match="no recorded baseline"):
        rb.lookup(data, "o/other")


# command line

def cli(argv, repos=None, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    code = rb.run(argv, fetch=FakeGitHub(repos or world()), token="t", out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def test_resolve_prints_only_the_sha_on_request():
    code, out, err = cli(["resolve", "--release", "3.6", "--repo", "opendatahub-io/kserve", "--format", "sha"])
    assert (code, out.strip(), err) == (0, SHARED, "")


def test_resolve_text_names_the_release_and_the_baseline():
    code, out, _ = cli(["resolve", "--release", "3.6", "--repo", "opendatahub-io/kserve"])
    assert code == 0
    assert f"baseline  {SHARED}" in out and f"release   {RELEASE_SHA}" in out
    assert "downstream-only" in out


def test_resolve_json_is_machine_readable():
    code, out, _ = cli(["resolve", "--release", "3.6", "--repo", "opendatahub-io/kserve", "--format", "json"])
    data = json.loads(out)
    assert code == 0 and data["baseline_sha"] == SHARED and data["release_ref"] == "rhoai-3.6"


def test_unresolved_exits_2_with_the_reason_on_stderr():
    code, out, err = cli(["resolve", "--release", "3.6", "--repo", "opendatahub-io/missing"])
    assert code == 2 and out == ""
    assert err.startswith("release-baseline: ")


def test_resolve_with_an_image_uses_its_label(tmp_path):
    repos = world()
    repos["opendatahub-io/kserve"]["compare"][sha("f")] = diverged(mb=sha("9"))

    def fetch(url, headers):
        if url.startswith("https://reg.example/"):
            return FakeRegistry({"vcs-ref": sha("f"), "vcs-url": "https://github.com/red-hat-data-services/kserve"})(url, headers)
        return FakeGitHub(repos)(url, headers)

    out, err = io.StringIO(), io.StringIO()
    code = rb.run(["resolve", "--release", "3.6", "--repo", "opendatahub-io/kserve", "--image",
                   "reg.example/org/kserve:tag", "--format", "sha"], fetch=fetch, token="t", auth_files=[], out=out, err=err)
    assert (code, out.getvalue().strip()) == (0, sha("9"))


def test_generate_then_lookup_round_trip(tmp_path):
    repos_file = tmp_path / "repos.txt"
    repos_file.write_text("opendatahub-io/kserve\n")
    output = tmp_path / "data" / "3.6.json"
    code, out, _ = cli(["generate", "--release", "3.6", "--repos-file", str(repos_file), "--output", str(output)])
    assert code == 0 and "1 resolved, 0 unresolved" in out
    code, out, _ = cli(["lookup", "--release", "3.6", "--repo", "opendatahub-io/kserve", "--data", str(output), "--format", "sha"])
    assert (code, out.strip()) == (0, SHARED)


def test_lookup_rejects_a_data_file_for_another_release(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"release": "3.5", "repos": {}}))
    code, _, err = cli(["lookup", "--release", "3.6", "--repo", "o/r", "--data", str(path)])
    assert code == 2 and "is for release 3.5" in err


def test_lookup_without_a_data_file_fails_cleanly(tmp_path):
    code, _, err = cli(["lookup", "--release", "3.6", "--repo", "o/r", "--data", str(tmp_path / "absent.json")])
    assert code == 2 and "cannot read" in err


# the checked-in data

DATA = ROOT / "data" / "release-baselines"


def test_checked_in_baselines_are_well_formed():
    data = json.loads((DATA / "3.6.json").read_text())
    assert data["release"] == "3.6"
    assert data["repos"]
    for repo, entry in data["repos"].items():
        assert repo.startswith("opendatahub-io/")
        for key in ("baseline_sha", "release_sha"):
            assert rb.FULL_SHA.fullmatch(entry[key]), (repo, key)
        assert entry["downstream_repo"].startswith(f"{rb.DOWNSTREAM_ORG}/")
        assert entry["release_ref"] == "rhoai-3.6"


def test_every_listed_pair_is_resolved_or_explained():
    data = json.loads((DATA / "3.6.json").read_text())
    pairs = dict(rb.read_pairs(DATA / "repos.txt"))
    assert set(pairs) == set(data["repos"]) | set(data["unresolved"])
    assert not set(data["repos"]) & set(data["unresolved"])
    for repo, entry in data["repos"].items():
        assert pairs[repo] == entry["downstream_repo"]


def test_readme_documents_every_field_of_the_data_file():
    data = json.loads((DATA / "3.6.json").read_text())
    readme = (ROOT / "cmd" / "release-baseline" / "README.md").read_text()
    fields = set(data) | {key for entry in data["repos"].values() for key in entry}
    undocumented = sorted(f for f in fields if f"`{f}`" not in readme)
    assert not undocumented, f"document these fields in cmd/release-baseline/README.md: {undocumented}"


def test_lookup_reads_the_checked_in_file_without_a_network():
    def no_network(url, headers):
        raise AssertionError("lookup must not use the network")

    repo = next(iter(json.loads((DATA / "3.6.json").read_text())["repos"]))
    out, err = io.StringIO(), io.StringIO()
    code = rb.run(["lookup", "--release", "3.6", "--repo", repo, "--format", "sha"], fetch=no_network, out=out, err=err)
    assert code == 0 and rb.FULL_SHA.fullmatch(out.getvalue().strip())
