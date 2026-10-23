---
name: onboard-api-contracts
description: Onboard a provider repository to the shared API contract checks. Inventories its OpenAPI documents and CRDs, drafts .rhoai/contracts.yaml, runs the compatibility and coverage checks locally, and prepares the report-mode CI workflow. Use when asked to onboard, add, or register a repository for API contract testing, or to confirm all of its OpenAPI specs are covered.
---

# Onboard a repository to the API contract checks

You are working in (or pointed at) a **provider repository** and have a checkout of
`red-hat-data-services/api-contract-tests` (call it `$ACT`). The runner is
`$ACT/cmd/contract-tests/cli.py`. Read `$ACT/docs/onboarding/README.md` first; this skill
is that checklist as an agent procedure.

Rollout is opt-in, starts in **report** mode, and the checks are pull-request jobs, not
promotion gates. Never describe them as gates.

## Ground rules

- Do not push, open a pull request, or comment on one unless the user asks. Preparing a
  branch and a commit locally is fine.
- Never guess a decision that belongs to the component owner: which APIs are contracts
  (`matchPath` scope), which documents are excluded, which commit is the baseline, and
  report versus enforce. Propose, show the evidence, and ask.
- Never put a mutable ref in a baseline. Use a full 40-character SHA from a trusted remote.
- Use the tool versions pinned in the descriptor (`oasdiff` and `yq`). Install into the
  provider's `bin/` or a temp dir, not system-wide:
  `GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@v<version>` and
  `github.com/mikefarah/yq/v4@v<version>`. Check whether the provider already has a pinned
  `bin/yq` first.
- Do not edit generated OpenAPI documents by hand. Regenerate them with the provider's own
  command.

## Procedure

### 1. Inventory

```bash
YQ=<path to pinned yq> python3 $ACT/cmd/contract-tests/cli.py discover --repo <provider> --format json
```

Read the result. `kind` is `bundled` (generated from a `src/` sibling), `source` (a bundle
input), or `standalone` (hand-maintained). `suggested_match_path` and `suggested_generator`
are heuristics. `bundler_script_candidates` are likely bundlers. Open the bundler script and
the `Makefile` target that produces each bundled document and confirm the adapter fits:

- `openapi.bundle.yq-merge` runs `bash <script> <entrypointArg>` in a temp copy that holds
  the script and the files matched by `sourceGlobs`. The script must only need those files.
  Compare `sourceGlobs` with what the real Makefile target depends on.
- `openapi.static` for hand-maintained documents.
- If neither fits, stop: a new adapter is a change to `api-contract-tests`, not something to
  copy into the provider.

### 2. Decide scope with the user

Present a table: each document, its kind, path count, and your proposed disposition
(contract, generator input, or exclusion with a reason). Ask about:

- Which APIs do consumers depend on, and which `matchPath` bounds each? A bundle can carry
  several APIs (for example plugin APIs that are out of scope).
- Which documents are not provider contracts (internal UI APIs, fixtures)? These need a
  `spec.exclusions` entry with a reason.
- Which baseline? A full SHA of the provider repository. The working group has not
  standardized EA versus minor release; ask which supported release the contract protects.
  Resolve it with `git rev-parse refs/remotes/<trusted-remote>/<branch>` and show the SHA
  and its subject line. An API that does not exist at the baseline is reported as `new`
  and is not compared, so a baseline that predates a new API is fine; confirm with the
  user when each API should become protected (when a baseline containing it is adopted).
- Does any single API need a different baseline from the rest (for example an agreed
  break)? Then give that contract a `baseline` block with a `reason`; do not move the
  repository default.

### 3. Draft the descriptor

Write `.rhoai/contracts.yaml` following `$ACT/docs/onboarding/openapi-compat.md` and
`$ACT/schemas/contract-set.schema.json`. One contract per API. Stable `id`s. Pin
`oasdiffVersion` and `yqVersion`. Add `severityOverrides` only with a stated reason (for
example promoting `response-optional-property-removed` to `warn` because a consumer reads an
optional field). Put every non-contract document under `spec.exclusions` with its reason.

### 4. Verify locally

For each contract:

```bash
python3 $ACT/cmd/contract-tests/cli.py check --repo <provider> \
  --contract-set .rhoai/contracts.yaml --contract-id <id> \
  --baseline-ref <full-sha> --output-dir /tmp/contract-report-<id>
```

Read `report.json.status`, not just the exit code (`new` means the API is absent at every baseline, so nothing was compared). Typical failures: the committed bundled
document is stale (regenerate it with the provider's command and commit that separately),
the baseline has no paths matching `matchPath` (wrong version or commit), or the generator
needs a file that `sourceGlobs` does not copy.

Then:

```bash
python3 $ACT/cmd/contract-tests/cli.py coverage --repo <provider> [--crd-glob '<glob>' ...]
```

It must report `Result: complete`. Resolve each `UNCOVERED` document by registering it or
excluding it with a reason. Do not loosen the check to make it pass.

If the repository ships CRDs, also run `crd-check` with the glob the workflow will use, and
pass that same glob to `coverage --crd-glob`.

### 5. Prepare CI

Create `.github/workflows/api-contract.yml` from the example in
`$ACT/docs/onboarding/openapi-compat.md` (step 5): a compatibility job (a matrix over
`contract-id` when there are several contracts) and a coverage job, both `mode: report`.
Pin the reusable workflows to a **full commit SHA that exists on `api-contract-tests` main**
(`git -C $ACT log -1 --format=%H origin/main`); confirm it contains
`provider-coverage-check.yml` and the per-contract artifact naming before using it. Add the
CRD job if applicable. Suggest a `CODEOWNERS` entry covering `.rhoai/` and the workflow.
Put a comment in the workflow explaining that the pinned SHAs are deliberate.

### 6. Hand back

Summarize for the user: contracts registered (id, `matchPath`, baseline SHA), exclusions and
their reasons, the `coverage` result, the local `check` results, and what you could not
verify (for example CI behavior you cannot run locally). List the decisions you asked about
and the answers. Offer to commit on a branch; do not open the pull request unless asked.

## Out of scope

Registering the contract with the working group (step 8 of the checklist), moving to
`enforce`, and making the job required for merges are owner decisions made later.
