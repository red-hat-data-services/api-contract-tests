# Onboarding a provider to the openapi-compat check

Start with the [onboarding checklist](README.md) for the whole path (inventory,
descriptor, local run, coverage, CI, code owners). This guide is the detail for
the OpenAPI check itself.

This generalizes the Model Catalog pilot's original checker into a reusable
module so a second provider never needs to copy or reimplement its logic. The
initial implementation came from the
[Model Catalog pilot](https://github.com/etirelli/rhoai-api-contract-wg/commit/293feebf1e780d7ce7b34b1669f5c937fa7b6338);
this document is the generic path for the next provider.

## 1. Confirm a generator adapter covers your bundling/generation step

Candidate generation is the one part of this check that is inherently
provider-specific — every repo builds its OpenAPI document differently.
Check [`modules/openapi-compat/adapters.py`](../../modules/openapi-compat/adapters.py)
for the currently registered adapters:

- **`openapi.bundle.yq-merge`** — your repo already has a shell script that
  merges source YAML files into a bundled document using `yq` (the pattern
  Model Catalog uses with `scripts/merge_catalog_specs.sh`).
- **`openapi.static`** — your OpenAPI document is hand-maintained,
  contract-first, with no generation step.

If neither fits, a new adapter must be added to `adapters.py` and reviewed
centrally — once added, every future repo with the same build pattern reuses
it for free. Do not copy checker logic into your own repository to work
around a missing adapter.

## 2. Write a `ContractSet` descriptor

Create `.rhoai/contracts.yaml` in your repository, validated against
[`schemas/contract-set.schema.json`](../../schemas/contract-set.schema.json).
Minimal shape:

```yaml
apiVersion: contracts.rhoai.io/v1alpha1
kind: ContractSet
metadata:
  name: your-service
spec:
  contracts:
    - id: your-service-v1
      type: openapi
      declaredArtifact: api/openapi/your-service.yaml
      matchPath: "^/api/your-service/v1(/|$)"
      policy:
        oasdiffVersion: "1.33.0"
        yqVersion: "4.45.1"
        flattenAllOf: true
        severityOverrides:
          response-optional-property-removed: warn
      generator:
        adapter: openapi.bundle.yq-merge
        script: scripts/your-bundler.sh
        yqEnvVar: YQ
        sourceDir: api/openapi/src
        sourceGlobs: ["your-service.yaml", "plugins/*.yaml"]
        entrypointArg: your-service.yaml
        output: api/openapi/your-service.yaml
```

Only paths matching `matchPath` are compared; this is how you scope a check
to one bounded interface within a larger bundled document instead of
accidentally gating on every endpoint in the repo. A repository with no
descriptor, or a document with no `contracts:` entry naming it, is never
checked.

`severityOverrides` lets you promote an oasdiff finding (see
`oasdiff checks changelog` for the full rule catalog) above its default
severity — for example, if consumers rely on an *optional* response field,
its removal defaults to `info` in oasdiff but can be promoted to `warn` so
it surfaces as a finding.

### Several contracts in one repository

A descriptor can register more than one contract, for example one per API
version. Give each a stable `id` and its own `matchPath`; the runner and the
reusable workflow check one contract at a time (`--contract-id`), so call the
workflow once per contract (see [step 5](#5-add-ci)).

### Documents that are not contracts

Every OpenAPI document in the repository must be accounted for. If one is
deliberately outside the contract set (an internal UI API, a test fixture),
list it with a reason instead of leaving it unmentioned:

```yaml
spec:
  exclusions:
    - path: clients/ui/bff/openapi/*
      reason: Internal UI back-end-for-front-end API, not a provider contract
  contracts:
    - ...
```

`path` is a repo-relative path or glob (`*` also matches `/`). An exclusion
without a `reason` is rejected. The [coverage check](#confirm-nothing-is-left-out)
reads this list.

## 3. Install the pinned tools

```sh
GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@<version pinned in your descriptor>
GOBIN="$PWD/bin" go install github.com/mikefarah/yq/v4@<version pinned in your descriptor>
```

`checker.py` rejects any other `oasdiff` or `yq` version. For `go install`
builds that print `main`, it checks the binary's embedded Go module version.

## 4. Run it locally

```bash
baseline_sha=$(git rev-parse refs/remotes/<trusted-remote>/main)
python3 <path-to-red-hat-data-services/api-contract-tests>/cmd/contract-tests/cli.py check \
  --repo . \
  --contract-set .rhoai/contracts.yaml \
  --baseline-ref "$baseline_sha" \
  --output-dir /tmp/contract-report
```

Repeat `--baseline-ref` for every applicable supported-release baseline;
every supplied baseline is compared independently. Never pass `main`,
`HEAD`, or an unresolved ref — only full, resolved Git SHAs. Add `--enforce`
once report-only evidence and owner agreement support gating merges.

Read `report.json.status`, not just the exit code: report-only mode exits
`0` even when breaking findings are recorded.

### Confirm nothing is left out

```bash
python3 <path-to-red-hat-data-services/api-contract-tests>/cmd/contract-tests/cli.py coverage \
  --repo . --contract-set .rhoai/contracts.yaml
```

The check finds every OpenAPI document tracked in the repository (plus
untracked files that are not git-ignored, so it works before you commit a new
spec) and reports each as one of:

| Status | Meaning |
|---|---|
| `contract` | The document is a contract's `declaredArtifact` |
| `source` | The document is an input of a registered generator (`sourceGlobs` or `source`) |
| `excluded` | Listed in `spec.exclusions`, with the reason shown |
| `UNCOVERED` | None of the above; the check fails |

It also fails on dangling configuration (a `declaredArtifact` that is not an
OpenAPI document, a `sourceGlob` that matches nothing) and warns about stale
exclusions. For each contract it reports how many API paths fall outside
`matchPath`; that is intentional scoping and never a failure, but it makes the
scope visible to reviewers. Add `--format json` for machine-readable output,
`--report-only` to see gaps without failing, and `--crd-glob` (repeatable) to
require that every CRD manifest is matched by the glob your CRD check uses.

## 5. Add CI

Call the reusable workflows from a provider workflow, for example
`.github/workflows/api-contract.yml`. Always reference them by a full commit
SHA; never by `main` or a tag. Start in `report` mode.

```yaml
name: API contract

on:
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  api-contract:
    strategy:
      fail-fast: false
      matrix:
        contract-id: [your-service-v1, your-service-v1alpha3]
    uses: red-hat-data-services/api-contract-tests/.github/workflows/provider-openapi-check.yml@<full-git-sha>
    with:
      contract-set: .rhoai/contracts.yaml
      contract-id: ${{ matrix.contract-id }}
      baseline-refs: <full-git-sha>
      oasdiff-version: "1.33.0"
      yq-version: "4.45.1"
      mode: report

  api-contract-coverage:
    uses: red-hat-data-services/api-contract-tests/.github/workflows/provider-coverage-check.yml@<full-git-sha>
    with:
      contract-set: .rhoai/contracts.yaml
      yq-version: "4.45.1"
      mode: report
```

Omit `contract-id` and the matrix when the descriptor registers exactly one
contract. Each contract uploads its own `contract-report-<id>` artifact.

The workflows validate their own reusable-workflow ref, install exact tool
versions, and reject mutable baseline refs. A change to `baseline-refs`, to the
pinned workflow SHA, or to the descriptor changes what the check protects, so
require an owner's review for those files (see the
[checklist](README.md#checklist), step 7).

## 6. Register the contract with the working group

Once proven locally, register the descriptor's location (repo, path,
revision) as a pointer so baselines, consumer profiles, and support level
can be tracked centrally. A descriptor existing in a repository does not
itself make that repository part of the gated cohort; registration is a
separate, reviewed step.
