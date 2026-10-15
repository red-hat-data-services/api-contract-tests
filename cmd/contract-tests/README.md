# contract-tests local runner

A thin CLI over the [`openapi-compat`](../../modules/openapi-compat/) and
[`crd-compat`](../../modules/crd-compat/) modules.

```bash
python3 cmd/contract-tests/cli.py check \
  --repo /path/to/provider/checkout \
  --contract-set .rhoai/contracts.yaml \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/contract-report \
  [--contract-id <id>] [--enforce] [--oasdiff <path>] [--yq <path>]
```

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
`protobuf-compat` and additional contract types are follow-on work.
