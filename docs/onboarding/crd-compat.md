# Onboarding a provider to the CRD compatibility check

See the [onboarding checklist](README.md) for the whole path. This guide is the
detail for the CRD check.

The CRD check compares CustomResourceDefinition YAML files in a provider pull
request against immutable Git baselines. It initially detects:

- removed CRDs and API versions;
- served or storage-version changes;
- removed schema properties;
- newly required properties;
- type changes, enum narrowing, and default changes.

The check is static and does not replace conversion, admission, or upgrade
tests. It gives provider pull requests an early, reproducible compatibility
signal without a cluster.

## Add the reusable workflow

Reference the workflow by a release tag, `stable`, `latest` or a full commit SHA
(see [releases and pinning](../releasing.md)); never by a branch:

```yaml
jobs:
  crd-contract:
    uses: red-hat-data-services/api-contract-tests/.github/workflows/provider-crd-check.yml@v0.1.0
    with:
      crd-glob: config/crd/**/*.yaml
      baseline-refs: <full-git-sha>
      mode: report
```

Start in `report` mode, review findings, then move to `enforce` once the
provider and consumers agree on the policy.

## Run locally

```bash
baseline_sha=$(git rev-parse refs/remotes/origin/main)
python3 <path-to-red-hat-data-services/api-contract-tests>/cmd/contract-tests/cli.py crd-check \
  --repo . \
  --crd-glob 'config/crd/**/*.yaml' \
  --baseline-ref "$baseline_sha" \
  --output-dir /tmp/crd-contract-report
```

Baselines must be full Git SHAs. Mutable refs such as `main`, tags, and branches
are rejected.

## New CRDs

A CRD that exists in the pull request but not in the baseline is new and is not
compared. When the baseline has no CRD manifests at all, the check reports
`status: new` and does not fail. If the baseline has CRD manifests but none match
your glob, that is a configuration error and still fails.

## The same CRD in several files

A provider may ship one CRD in more than one place, for example a copy per
kustomize overlay. Identical copies are accepted and compared once. If two files
matched by the glob define the same CRD (same group and kind) differently, the
check fails with a configuration error naming both files, because otherwise only
one copy would be compared and a breaking change in the other could go unnoticed.
Keep the copies identical (ideally generated from one source), or narrow the glob
to one of them.

## Confirm every CRD is covered

The glob decides which manifests are compared, so a CRD that moves or is added
outside it silently leaves the check. Run the coverage check with the same
glob:

```bash
python3 <path-to-red-hat-data-services/api-contract-tests>/cmd/contract-tests/cli.py coverage \
  --repo . --contract-set .rhoai/contracts.yaml --crd-glob 'config/crd/**/*.yaml'
```

Every CRD manifest must match a `--crd-glob` (repeat the flag when you use
several), and every glob must match at least one manifest. Kustomize patches
that merely say `kind: CustomResourceDefinition` (conversion-webhook or
CA-injection patches) are not CRD manifests and are ignored. CRDs that are
deliberately not checked, such as mocked CRDs used as test data, are named with
`--crd-exclude <glob>`; keep the reason in a comment next to the workflow
input so reviewers can read it.

A repository that ships only CRDs has no descriptor. In that case `coverage`
runs in CRD-only mode: pass `--crd-glob` and it checks just the CRD globs. If
the repository also has OpenAPI documents, it needs a descriptor as usual.

## Follow-on work

The static schema check is intentionally the first CRD increment. Conversion,
stored-object migration, and consumer-reliance profiles require bounded
`envtest` scenarios and remain follow-on work.
