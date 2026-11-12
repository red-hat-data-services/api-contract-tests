---
name: onboard-api-contracts
description: Onboard a provider repository to the shared API contract checks. Inventories its OpenAPI documents and CRDs, drafts .rhoai/contracts.yaml, runs the compatibility and coverage checks locally, and prepares the enforce-mode CI workflow. Use when asked to onboard, add, or register a repository for API contract testing, or to confirm all of its OpenAPI specs are covered.
---

# Onboard a repository to the API contract checks

You are working in (or pointed at) a **provider repository** and have a checkout of
`red-hat-data-services/api-contract-tests` (call it `$ACT`). The runner is
`$ACT/cmd/contract-tests/cli.py`. Read `$ACT/docs/onboarding/README.md` first; this skill
is that checklist as an agent procedure.

Rollout is opt-in, the CI jobs run in **`mode: enforce`** (a pull request that breaks a
registered contract fails the job; enforce is the default, and the workflow still sets
`mode: enforce` explicitly), and the checks are pull-request jobs, not promotion
gates. Never describe them as gates.

## Ground rules

- Do not push, open a pull request, or comment on one unless the user asks. Preparing a
  branch and a commit locally is fine.
- Never guess a decision that belongs to the component owner: which APIs are contracts
  (`matchPath` scope), which documents are excluded, and which commit is the baseline.
  Propose, show the evidence, and ask. The mode is not a question: always use
  `mode: enforce`, and do not offer report mode.
- Never put a mutable ref in a baseline. Use a full 40-character SHA from a trusted remote.
- Use the tool versions pinned in the descriptor (`oasdiff` and `yq`). Install into the
  provider's `bin/` or a temp dir, not system-wide:
  `GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@v<version>` and
  `github.com/mikefarah/yq/v4@v<version>`. Check whether the provider already has a pinned
  `bin/yq` first.
- If `discover` reports a document with `dialect: swagger-2.0`, also build the converter oasdiff
  needs for it: `go build -C $ACT/cmd/swagger2openapi -o "<provider>/bin/swagger2openapi" .`, and
  pass it to `check` with `--swagger2openapi`.
- `matchPath` is matched against the full URL path: the base path (Swagger `basePath`, or the
  path of the OpenAPI 3 `servers` URLs) plus the path. If `discover` reports a
  `base_path_error` (servers with different paths), raise it with the owner; the check fails
  until the servers agree. A Swagger 2.0 file with no `paths` (SDK definitions, for example) is
  not a contract: exclude it with a reason. See `$ACT/docs/onboarding/openapi-compat.md`.
- Do not edit generated OpenAPI documents by hand. Regenerate them with the provider's own
  command.

## Baseline SHA

The baseline is the most important value in the configuration. Explain it to the
user in plain words before you ask for it:

- The baseline commit defines what counts as **released**. Everything that exists
  there must stay compatible in every later pull request. Anything that does not
  exist there is reported as `new` and is not compared.
- A baseline that is **too new** (for example a commit after a breaking change)
  silently accepts breaks to APIs that already shipped: the check passes and the
  consumers break later.
- A baseline that is **too old, or from another release line**, makes every
  intentional change since then look like a break. The check fails on every pull
  request and teams stop trusting it.
- The baseline is a promise. Moving it forward is an exception that someone other
  than the author must approve, so a wrong choice is expensive to undo.

Suggest the baseline in this order, and always let the user confirm it:

1. **The commit the RHOAI 3.6 release was based on.** Do not search for tags: the
   downstream `red-hat-data-services` repositories have no 3.6 tags, only a
   `rhoai-3.6` branch, and its head is a downstream-only commit that does not exist
   in the `opendatahub-io` repository the check runs in, so it would fail there. The
   baseline is the newest commit both repositories share (the merge base of
   `rhoai-3.6` and the default branch). Get it with
   `python3 $ACT/cmd/release-baseline/release_baseline.py lookup --release 3.6 --repo <org>/<repo>`,
   which reads `$ACT/data/release-baselines/3.6.json` and needs no network. If the
   repository is not listed, or the user wants to verify or refresh the value, run
   `resolve` instead of `lookup` (GitHub API; set `GITHUB_TOKEN` or log in with
   `gh`). Add `--image <release image>` to take the release commit from the image's
   source-commit label instead of the branch head. Show the user the baseline SHA,
   the release commit it was derived from and the data file's `generated` date, and
   ask them to confirm.
2. **Otherwise, the current head of `main`** (the repository's default branch):
   `git rev-parse refs/remotes/<trusted-remote>/main`. Use this when the
   `lookup` and `resolve` report no baseline (for example a repository with no
   downstream counterpart or no shared history) or the user cannot confirm one. Tell the
   user what it means: everything on `main` today is treated as released and
   protected from now on, earlier releases are not compared, and anything already
   broken on `main` is accepted.

Rules that apply either way:

- Record a full 40-character SHA resolved from a trusted remote, never from a fork
  or an unpushed local branch. Never write `main`, a branch, a tag or `HEAD`; the
  runner rejects them.
- Use the same SHA everywhere the repository default is set: the workflow's
  `baseline-refs` input, `--baseline-ref` in local runs, and `spec.baseline.refs`
  if the descriptor carries one.
- Next to the setting, record the SHA's source (for example "merge base of
  rhoai-3.6 and main, from release_baseline.py on <date>", or "`main` on <date>")
  in a comment so a reviewer can verify it.

The same guidance is in `$ACT/docs/onboarding/README.md` (Choosing a baseline); keep
the two consistent.

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
- Which baseline? Follow **Baseline SHA** above: explain why it matters, propose the
  commit the 3.6 release was based on (or the current `main` SHA when there is none),
  show the full SHA with its source and subject line, and ask the user to confirm.
  An API that does not exist at the baseline is reported as `new` and is not compared,
  so a baseline that predates a new API is fine; confirm with the user when each API
  should become protected (when a baseline containing it is adopted).
- Does any single API need a different baseline from the rest (for example an agreed
  break)? Then give that contract a `baseline` block with a `reason`; do not move the
  repository default.

### 3. Draft the descriptor

Write `.rhoai/contracts.yaml` following `$ACT/docs/onboarding/openapi-compat.md` and
`$ACT/schemas/contract-set.schema.json`. One contract per API. Stable `id`s. Pin
`oasdiffVersion` and `yqVersion`. Add `severityOverrides` only with a stated reason (for
example promoting `response-optional-property-removed` to `warn` because a consumer reads an
optional field). Put every non-contract document under `spec.exclusions` with its reason.
If that leaves no contracts (for example a CRD-driven operator whose only OpenAPI document is
a definitions-only `swagger.json` generated from its CRDs for an SDK), write `contracts: []`
and keep the exclusions: skip the per-contract `check` and the compatibility job, run
`coverage` with `--yq-version <pinned yq>` and the CRD globs, and add only the CRD and
coverage jobs. Confirm with the user that no document is a servable API before doing so.

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

If `check` fails because the repository already broke its baseline, that is a real
finding: show it to the user, and fix the break or, if it was intentional and agreed,
record a per-contract `baseline` override with a `reason`. Do not switch to report
mode to get a green run.

Then:

```bash
python3 $ACT/cmd/contract-tests/cli.py coverage --repo <provider> [--crd-glob '<glob>' ...]
```

It must report `Result: complete`. Resolve each `UNCOVERED` document by registering it or
excluding it with a reason. Do not loosen the check to make it pass.

If the repository ships CRDs, also run `crd-check` (enforce mode is the default) with the glob the workflow will
use, and pass that same glob to `coverage --crd-glob`.

### 5. Prepare CI

Create `.github/workflows/api-contract.yml` from the example in
`$ACT/docs/onboarding/openapi-compat.md` (step 5): a compatibility job (a matrix over
`contract-id` when there are several contracts) and a coverage job, both `mode: enforce`.
Set `baseline-refs` to the confirmed baseline SHA, with the source comment described
in **Baseline SHA**.
Pin the reusable workflows to **`@latest`**, unless the user asks for a version tag,
`stable` or a full commit SHA; `$ACT/docs/releasing.md` explains the choices.
Never pin to a branch. Confirm the release `latest` points to contains `provider-coverage-check.yml` and the
per-contract artifact naming before using it. Add the
CRD job if applicable. Suggest a `CODEOWNERS` entry covering `.rhoai/` and the workflow.
Put a comment in the workflow naming the pinned release and pointing to `docs/releasing.md` for upgrades.

### 6. Hand back

Summarize for the user: contracts registered (id, `matchPath`, baseline SHA), where the baseline came from
(the 3.6 release base or current `main`) and that the user confirmed it, exclusions and
their reasons, the `coverage` result, the local `check` results, and what you could not
verify (for example CI behavior you cannot run locally). List the decisions you asked about
and the answers. Offer to commit on a branch; do not open the pull request unless asked.

## Out of scope

Registering the contract with the working group (step 8 of the checklist) and making
the job required for merges (branch protection) are owner decisions made later.
