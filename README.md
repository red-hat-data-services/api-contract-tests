# api-contract-tests

Shared CI contract tests for RHOAI and ODH provider repositories, hosted at
[`red-hat-data-services/api-contract-tests`](https://github.com/red-hat-data-services/api-contract-tests).

`api-contract-tests` is the canonical home for the reusable compatibility runner,
descriptor schemas, provider workflows, and onboarding guides. Provider
repositories keep their canonical OpenAPI documents, CRDs, and behavior tests;
consumer repositories keep their reliance profiles and consumer-side tests. This
repository supplies the shared checks and result format.

## Current status

- The OpenAPI compatibility module is implemented and tested, for OpenAPI 3 and Swagger 2.0 contracts.
- The initial static CRD compatibility module is implemented and tested.
- The coverage check (`coverage`) confirms every OpenAPI document and CRD in a
  repository is registered; `discover` inventories a repository for onboarding.
- The local runner is `cmd/contract-tests/cli.py`.
- Provider onboarding is opt-in and runs in enforce mode, the default for the
  reusable workflows and the CLI; see
  [`docs/onboarding`](docs/onboarding/README.md). Providers pin the reusable
  workflows to `@latest` (a version tag, `@stable` or a full commit SHA also
  work); see [releases and pinning](docs/releasing.md). Check each provider
  repository's workflow for whether it has been wired in yet.
- Consumer onboarding is repository-local today: the consumer owns the reliance
  profile and tests. See the [consumer guide](docs/onboarding/consumer-compat.md).
- CRD conversion, stored-object migration, and shared consumer-reliance
  verification are follow-on work.

## Quick start

From a provider repository with a `.rhoai/contracts.yaml` descriptor:

```bash
python3 /path/to/api-contract-tests/cmd/contract-tests/cli.py check \
  --repo . \
  --contract-set .rhoai/contracts.yaml \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/contract-report
```

Read `report.json.status`, not only the process exit code. Enforce mode is the
default and the recommended mode (the workflows' `mode: enforce`): a breaking finding
fails the run. With `--report-only` the findings are recorded and the run still exits
0 (report mode, not recommended). Infrastructure and integrity errors fail closed either way.

Every check pins `oasdiff` and `yq` in the provider descriptor and resolves
each baseline to a full Git SHA. Mutable refs such as `main`, `HEAD`, tags,
and branches are rejected.

The baseline decides what the check protects, so choose it deliberately: the
commit the RHOAI 3.6 release was based on, or the SHA that `main` resolves to
today when there is none. See
[Choosing a baseline](docs/onboarding/README.md#choosing-a-baseline) for why it
matters and how to find the commit.

Before relying on the check, confirm every OpenAPI document in the repository
is registered (or excluded with a reason):

```bash
python3 /path/to/api-contract-tests/cmd/contract-tests/cli.py coverage --repo .
```

New to the repository? `discover --repo .` inventories its OpenAPI documents
and CRDs and suggests a contract for each.

For a provider with committed CRD manifests:

```bash
python3 /path/to/api-contract-tests/cmd/contract-tests/cli.py crd-check \
  --repo . \
  --crd-glob 'config/crd/**/*.yaml' \
  --baseline-ref <full-git-sha> \
  --output-dir /tmp/crd-contract-report
```

See [CRD onboarding](docs/onboarding/crd-compat.md) for the initial static
policy and the remaining cluster-backed follow-on work.

For the consumer side, start with the
[consumer onboarding guide](docs/onboarding/consumer-compat.md).

## Repository layout

```text
.
├── cmd/contract-tests/       # Local runner
├── cmd/release-baseline/     # Finds the commit an RHOAI release was based on (baseline SHA)
├── cmd/swagger2openapi/      # Converts Swagger 2.0 contracts to OpenAPI 3 for oasdiff (Go)
├── modules/openapi-compat/   # OpenAPI compatibility engine
├── modules/crd-compat/       # Static CRD compatibility engine
├── modules/coverage/         # Coverage check and repository discovery
├── schemas/                  # ContractSet descriptor schema
├── data/release-baselines/   # Generated release baselines, per release
├── .claude/skills/           # Assistant skills for provider and consumer onboarding
├── docs/
│   ├── architecture.md       # Ownership and enforcement model
│   ├── onboarding/           # Provider and consumer onboarding checklists and guides
│   ├── releasing.md          # Pinning options and the release process
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
OASDIFF=/path/to/oasdiff YQ=/path/to/yq SWAGGER2OPENAPI=/path/to/swagger2openapi python3 -m pytest modules/openapi-compat/tests modules/crd-compat/tests modules/coverage/tests cmd/action-pins/tests cmd/workflow-steps/tests cmd/release-baseline/tests -v
```

CI also lints the workflows with [actionlint](https://github.com/rhysd/actionlint)
v1.7.12 (settings in `.github/actionlint.yaml`) and checks that every action's
pinned SHA is the commit its `# vX.Y.Z` comment names:

```bash
GOBIN="$PWD/bin" go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
bin/actionlint
python3 cmd/action-pins/check_action_pins.py
```

The runner and modules are type-checked with mypy (settings in `mypy.ini`). Each
module directory has its own `checker.py`, so check one file at a time:

```bash
for file in cmd/contract-tests/cli.py cmd/release-baseline/release_baseline.py modules/*/*.py; do python3 -m mypy "$file"; done
```

The provider workflows keep their logic in `cmd/workflow-steps/steps.py`
(tested by `cmd/workflow-steps/tests`); the YAML only wires steps together. The
`Workflow self-test` workflow (`.github/workflows/self-test.yml`) runs all three
provider workflows end to end on every pull request, against the fixture provider
in [`selftest/`](selftest/README.md).

The test suite requires `oasdiff` v1.33.0, `yq` v4.45.1 and the Swagger 2.0 converter:

```bash
GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@v1.33.0
GOBIN="$PWD/bin" go install github.com/mikefarah/yq/v4@v4.45.1
go build -C cmd/swagger2openapi -o "$PWD/bin/swagger2openapi" .
go test -C cmd/swagger2openapi ./...
```

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
