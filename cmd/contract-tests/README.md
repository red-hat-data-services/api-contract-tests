# contract-tests local runner

A thin CLI over the [`openapi-compat`](../../modules/openapi-compat/) module.
It is intentionally a Python script until a second contract type justifies a
compiled runner.

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

Only `openapi` contracts are implemented. `protobuf-compat`, `crd-compat`,
and additional contract types are follow-on work.
