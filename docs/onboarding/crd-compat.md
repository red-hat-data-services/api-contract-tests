# Onboarding a provider to the CRD compatibility check

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

Reference the workflow by a full commit SHA:

```yaml
jobs:
  crd-contract:
    uses: red-hat-data-services/contract-tests/.github/workflows/provider-crd-check.yml@<full-git-sha>
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
python3 <path-to-red-hat-data-services/contract-tests>/cmd/contract-tests/cli.py crd-check \
  --repo . \
  --crd-glob 'config/crd/**/*.yaml' \
  --baseline-ref "$baseline_sha" \
  --output-dir /tmp/crd-contract-report
```

Baselines must be full Git SHAs. Mutable refs such as `main`, tags, and branches
are rejected.

## Follow-on work

The static schema check is intentionally the first CRD increment. Conversion,
stored-object migration, and consumer-reliance profiles require bounded
`envtest` scenarios and remain follow-on work.
