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
  [--contract-id <id>] [--oasdiff <path>] [--yq <path>]
```

`--baseline-ref` is the repository default and is optional when the descriptor
declares `spec.baseline.refs` or the contract its own `baseline`. An API absent
at a default baseline is reported as `new`, not a failure. Use the commit the RHOAI
3.6 release was based on, or the SHA that `main` resolves to today when there is
none; see
[`docs/onboarding`](../../docs/onboarding/README.md#choosing-a-baseline) for why it
matters.

`check` converts a Swagger 2.0 baseline or candidate to OpenAPI 3 before comparing, using the
`swagger2openapi` converter (`--swagger2openapi` or `SWAGGER2OPENAPI`; build it from
[`cmd/swagger2openapi`](../swagger2openapi/README.md)). It is needed only for Swagger 2.0 documents.

`check`, `crd-check`, and `coverage` run in enforce mode by default, as the reusable
workflows do: a breaking finding or coverage gap exits 1. `--report-only` records
findings and exits 0 instead (report mode, not recommended); integrity errors still
fail. `--enforce` is still accepted for compatibility but is no longer needed.

The same command runs locally and in CI — there is no separate "CI mode" —
so a result is always reproducible without a cluster. See
[`docs/onboarding/openapi-compat.md`](../../docs/onboarding/openapi-compat.md).

CRD checks use:

```bash
python3 cmd/contract-tests/cli.py crd-check \
  --repo /path/to/provider/checkout \
  --crd-glob 'config/crd/**/*.yaml' \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/crd-contract-report
```

See [`docs/onboarding/crd-compat.md`](../../docs/onboarding/crd-compat.md).
Coverage and discovery:

```bash
# Fail if an OpenAPI document (or, with --crd-glob, a CRD) is not registered.
python3 cmd/contract-tests/cli.py coverage \
  --repo /path/to/provider/checkout \
  [--contract-set .rhoai/contracts.yaml] [--crd-glob 'config/crd/**/*.yaml'] \
  [--baseline-ref <full-sha>] \
  [--format text|json] [--report-only] [--yq <path>] [--yq-version <version>]

# Inventory a repository and suggest contract shapes; needs no descriptor.
python3 cmd/contract-tests/cli.py discover --repo /path/to/provider/checkout [--format text|json]
```

`coverage` exits 0 when everything is accounted for, 1 when it finds gaps (0 with
`--report-only`), and 2 on an integrity error such as an unparseable or malformed descriptor
(both `check` and `coverage` enforce `schemas/contract-set.schema.json` the same way),
an exclusion without a reason, or a `yq` that does not match the pinned version.
The pin is each contract's `policy.yqVersion`, or `--yq-version`, which must agree
with it; a descriptor with no contracts pins nothing, so pass `--yq-version` (the
reusable workflow does) or coverage fails with an integrity error rather than run an
unverified `yq`. `spec.contracts` is required; use `contracts: []` for a descriptor
with only exclusions.

`protobuf-compat` and additional contract types are follow-on work.
