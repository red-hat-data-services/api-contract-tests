# contract-tests local runner

A thin CLI over the [`openapi-compat`](../../modules/openapi-compat/),
[`crd-compat`](../../modules/crd-compat/), and
[`coverage`](../../modules/coverage/) modules.

```bash
python3 cmd/contract-tests/cli.py check \
  --repo /path/to/provider/checkout \
  --contract-set .rhoai/contracts.yaml \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/contract-report \
  [--contract-id <id>] [--enforce] [--oasdiff <path>] [--yq <path>]
```

`--baseline-ref` is the repository default and is optional when the descriptor
declares `spec.baseline.refs` or the contract its own `baseline`. An API absent
at a default baseline is reported as `new`, not a failure; see
[`docs/onboarding`](../../docs/onboarding/README.md#choosing-a-baseline).

The same command runs locally and in CI — there is no separate "CI mode" —
so a result is always reproducible without a cluster. See
[`docs/onboarding/openapi-compat.md`](../../docs/onboarding/openapi-compat.md).

CRD checks use:

```bash
python3 cmd/contract-tests/cli.py crd-check \
  --repo /path/to/provider/checkout \
  --crd-glob 'config/crd/**/*.yaml' \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/crd-contract-report \
  [--enforce]
```

See [`docs/onboarding/crd-compat.md`](../../docs/onboarding/crd-compat.md).
Coverage and discovery:

```bash
# Fail if an OpenAPI document (or, with --crd-glob, a CRD) is not registered.
python3 cmd/contract-tests/cli.py coverage \
  --repo /path/to/provider/checkout \
  [--contract-set .rhoai/contracts.yaml] [--crd-glob 'config/crd/**/*.yaml'] \
  [--baseline-ref <full-sha>] \
  [--format text|json] [--report-only] [--yq <path>]

# Inventory a repository and suggest contract shapes; needs no descriptor.
python3 cmd/contract-tests/cli.py discover --repo /path/to/provider/checkout [--format text|json]
```

`coverage` exits 0 when everything is accounted for, 1 when it finds gaps (0 with
`--report-only`), and 2 on an integrity error such as an unparseable descriptor,
an exclusion without a reason, or an unpinned `yq`.

`protobuf-compat` and additional contract types are follow-on work.
