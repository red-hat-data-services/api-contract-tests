# contract-tests

Shared CI contract tests for RHOAI and ODH provider repositories, hosted at
[`red-hat-data-services/contract-tests`](https://github.com/red-hat-data-services/contract-tests).

`contract-tests` is the canonical home for the reusable compatibility runner,
descriptor schemas, provider workflows, and onboarding guides. Provider
repositories keep their canonical OpenAPI documents, CRDs, and behavior tests;
this repository supplies the shared checks and result format.

## Current status

- The OpenAPI compatibility module is implemented and tested.
- The initial static CRD compatibility module is implemented and tested.
- The local runner is `cmd/contract-tests/cli.py`.
- The reusable provider workflow is drafted but not yet wired into a live provider repository.
- CRD conversion, stored-object migration, and consumer-reliance checks are follow-on work.

## Quick start

From a provider repository with a `.rhoai/contracts.yaml` descriptor:

```bash
python3 /path/to/contract-tests/cmd/contract-tests/cli.py check \
  --repo . \
  --contract-set .rhoai/contracts.yaml \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/contract-report
```

Read `report.json.status`, not only the process exit code. Report-only mode
records breaking findings without failing the build; infrastructure and
integrity errors still fail closed.

Every check pins `oasdiff` and `yq` in the provider descriptor and resolves
each baseline to a full Git SHA. Mutable refs such as `main`, `HEAD`, tags,
and branches are rejected.

For a provider with committed CRD manifests:

```bash
python3 /path/to/contract-tests/cmd/contract-tests/cli.py crd-check \
  --repo . \
  --crd-glob 'config/crd/**/*.yaml' \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/crd-contract-report
```

See [CRD onboarding](docs/onboarding/crd-compat.md) for the initial static
policy and the remaining cluster-backed follow-on work.

## Repository layout

```text
.
├── cmd/contract-tests/       # Local runner
├── modules/openapi-compat/   # OpenAPI compatibility engine
├── modules/crd-compat/       # Static CRD compatibility engine
├── schemas/                  # ContractSet descriptor schema
├── docs/
│   ├── architecture.md       # Ownership and enforcement model
│   ├── onboarding/           # Provider onboarding guides
│   └── research/             # CRD and follow-on research
└── .github/workflows/        # CI and provider workflows
```

## Ownership model

- Provider repositories own canonical contracts and provider behavior tests.
- Consumer repositories own their reliance profiles.
- This repository owns shared checks, schemas, workflows, policy, and the pointer registry.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
OASDIFF=/path/to/oasdiff YQ=/path/to/yq python3 -m pytest modules/openapi-compat/tests modules/crd-compat/tests -v
```

The test suite requires `oasdiff` v1.33.0 and `yq` v4.45.1:

```bash
GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@v1.33.0
GOBIN="$PWD/bin" go install github.com/mikefarah/yq/v4@v4.45.1
```

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
