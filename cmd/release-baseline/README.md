# release-baseline

Finds the commit an RHOAI release was based on, so onboarding can use it as the
baseline SHA. See [Choosing a baseline](../../docs/onboarding/README.md#choosing-a-baseline)
for why the baseline matters and when to fall back to the SHA `main` resolves to.

```bash
# From the checked-in data (no network)
python3 cmd/release-baseline/release_baseline.py lookup  --release 3.6 --repo opendatahub-io/odh-dashboard

# From GitHub (set GITHUB_TOKEN or GH_TOKEN, or log in with `gh`)
python3 cmd/release-baseline/release_baseline.py resolve --release 3.6 --repo opendatahub-io/odh-dashboard

# Only the SHA, for scripts
python3 cmd/release-baseline/release_baseline.py lookup  --release 3.6 --repo opendatahub-io/odh-dashboard --format sha
```

Exit status is 0 when a baseline was found and 2 when it was not (the message says
why). When there is no baseline, use the SHA that the repository's default branch
resolves to.

## What it returns

The baseline is the merge base of the downstream release branch
(`red-hat-data-services/<name>`, branch `rhoai-<release>`) and the upstream
repository's default branch: the newest commit that both histories share. It is
not the head of the release branch.

Measured on 2026-11-09 across the 70 repositories in the release source map that
have an `opendatahub-io` counterpart:

- Every downstream repository has a `rhoai-3.6` branch (74 of 74) and none has a 3.6
  tag.
- No repository has the same `main` SHA downstream and upstream (40 downstream
  `main` branches contain upstream plus extra commits, 27 have diverged, 3 could not
  be compared).
- The head of `rhoai-3.6` is not a branch or tag SHA upstream in any repository, and
  in the 68 repositories where the histories can be compared it is not in upstream
  `main`'s history. A baseline taken from it would not exist in the repository where
  the check runs.
- The merge base does exist in both, and it stays put while the release branch head
  moves (release branches receive bot commits; `rhods-operator` got two within
  minutes of each other during testing). It only advances when new upstream commits
  are merged into the release branch.

Two repositories (`modelexpress`, `kserve-autogluon-server`) are standalone upstream
and have no shared history with their downstream copy, so they have no baseline here.

## Release image labels

`--image <reference>` takes the release commit from the image's source-commit label
(`vcs-ref`, falling back to `git.commit` and `io.openshift.build.commit.id`) instead
of the branch head, then relates it to the upstream repository the same way. The
label is what actually shipped. `org.opencontainers.image.revision` is ignored on
purpose: base images pass their own value through, and on the ODH operator image it
names a commit that is not in the repository.

Registry credentials are read from `REGISTRY_AUTH_FILE`,
`~/.config/containers/auth.json` or `~/.docker/config.json`, only for the image's
registry, and are never printed.

This path is tested against public `quay.io/opendatahub` images and with a fake
registry. It has not been run against the RHOAI release images: on 2026-11-09 the
pull credentials used for testing were refused (401) by `quay.io/rhoai` and
`registry.redhat.io`, so how those images are labeled is unverified.

## Regenerating the data

[`data/release-baselines/3.6.json`](../../data/release-baselines/3.6.json) is the
output of `generate` for the pairs in
[`repos.txt`](../../data/release-baselines/repos.txt). It is a snapshot (see its
`generated` field). Regenerate it for a new release or when upstream is synced into
the release branch:

```bash
python3 cmd/release-baseline/release_baseline.py generate --release 3.7 \
  --repos-file data/release-baselines/repos.txt --output data/release-baselines/3.7.json
```

## Development

The standard library only. GitHub and the registry are faked in the tests.

```bash
python3 -m pytest cmd/release-baseline/tests -v
python3 -m mypy cmd/release-baseline/release_baseline.py
```
