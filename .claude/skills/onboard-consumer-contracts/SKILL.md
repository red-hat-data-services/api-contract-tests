---
name: onboard-consumer-contracts
description: Onboard a consumer repository to the shared RHOAI and ODH API contract checks. Inventories the operations, response fields, status codes, and behavior a consumer relies on; drafts a consumer reliance profile; verifies consumer-side contract tests; and prepares enforce-mode CI. Use when asked to onboard a consumer, record consumer reliance, or check a consumer against a provider OpenAPI contract.
---

# Onboard a consumer repository to the API contract checks

You are working in (or pointed at) a **consumer repository** and have a checkout
of `red-hat-data-services/api-contract-tests` (call it `$ACT`). Read
`$ACT/docs/onboarding/consumer-compat.md` first; this skill is that guide as an
agent procedure.

The provider owns the canonical contract and provider behavior tests. The
consumer owns the reliance profile and consumer-side tests. Rollout is opt-in,
the consumer CI runs in **enforce** mode, and the consumer check is not a provider merge gate.

## Ground rules

- Never guess what the consumer relies on. Derive it from code and tests, then
  ask the consumer owner to confirm the operations, response fields, status
  codes, behavior, support level, and provider baseline.
- Never copy the provider OpenAPI document, generated artifact, or provider
  tests into the consumer repository. Reference them by repository, path, and
  ref.
- Use a full 40-character SHA for the recorded provider baseline. `main` is a
  drift signal only; never record the name `main` as the baseline. If there is no
  3.6 release commit to use, record the full SHA that provider `main` resolves to
  today (see **Provider baseline SHA**).
- Do not add a cluster requirement or full end-to-end deployment to the consumer
  check. Use the consumer's existing boundary and test harness.
- Do not push, open a pull request, or comment on one unless the user asks.
  Preparing a branch and a local commit is fine.

## Provider baseline SHA

The provider baseline is the most important value in the reliance profile. Explain
it to the user in plain words before you ask for it:

- It pins the provider version that the consumer's tests and reliance profile are
  checked against. A passing run only proves something about that version.
- If it is not the provider version the consumer actually ships against, the
  result misleads. A baseline that is **too old** means the consumer is never
  tested against current provider behavior. A baseline that is **too new** (for
  example a commit after a breaking change) means it is tested against behavior
  the shipped provider does not have, or hides a break that already shipped.
- The baseline is a promise. Moving it later needs review from someone other than
  the author, so a wrong choice is expensive to undo.

Suggest the baseline in this order, and always let the user confirm it:

1. **The provider commit the RHOAI 3.6 release was based on.** Do not search for
   tags: the downstream `red-hat-data-services` repositories have no 3.6 tags, only
   a `rhoai-3.6` branch, and its head is a downstream-only commit that does not
   exist in the `opendatahub-io` provider repository. The baseline is the newest
   commit both repositories share (the merge base of `rhoai-3.6` and the provider's
   default branch). Get it with
   `python3 $ACT/cmd/release-baseline/release_baseline.py lookup --release 3.6 --repo <org>/<provider-repo>`,
   which reads `$ACT/data/release-baselines/3.6.json` and needs no network. If the
   provider is not listed, or the user wants to verify or refresh the value, run
   `resolve` instead of `lookup` (GitHub API; set `GITHUB_TOKEN` or log in with
   `gh`). Add `--image <release image>` to take the release commit from the image's
   source-commit label instead of the branch head. Show the user the baseline SHA,
   the release commit it was derived from and the data file's `generated` date, and
   ask them to confirm.
2. **Otherwise, the current head of the provider's `main`** (its default branch):
   `git rev-parse refs/remotes/<trusted-remote>/main`. Use this when the provider
   `lookup` and `resolve` report no baseline (for example a provider with no
   downstream counterpart or no shared history) or the user cannot confirm one. Tell the user what it
   means: the provider as it is today is treated as the version the consumer
   relies on, earlier releases are not covered, and the drift run in step 5 only
   shows provider changes made after this SHA.

If the provider's `.rhoai/contracts.yaml` already declares a baseline, show it next
to your suggestion so the user can see whether the two differ.

Rules that apply either way:

- Record a full 40-character SHA resolved from a trusted remote, never from a fork
  or an unpushed local branch. Never write `main`, a branch, a tag or `HEAD`.
- Use the same SHA in the reliance profile and in the pinned provider checkout the
  tests run against (step 5), and wherever the CI workflow pins the provider version.
- Next to the setting, record the SHA's source (for example "merge base of
  rhoai-3.6 and main, from release_baseline.py on <date>", or "`main` on <date>")
  in a comment so a reviewer can verify it.

The same guidance is in `$ACT/docs/onboarding/README.md` (Choosing a baseline) and
`$ACT/docs/onboarding/consumer-compat.md` (Choosing the provider reference); keep
them consistent.

## Procedure

### 1. Identify the provider boundary

Read the provider's `.rhoai/contracts.yaml` and record:

- provider repository and owner;
- contract ID and declared artifact path;
- API version and support level the consumer claims;
- full-SHA baseline and artifact digest, if the provider declares them (see
  **Provider baseline SHA** for what to propose to the user);
- the consumer entry point that calls the provider (BFF client, SDK, adapter, or
  service client).

If the provider has no registered contract, stop and point the user to the
provider onboarding path. Do not invent a contract ID or baseline.

### 2. Inventory reliance from the consumer

Search the consumer repository for provider URL paths, route constants,
generated clients, operation IDs, request parameters, headers, response fields,
status codes, error shapes, pagination, and retry behavior. Read the tests and
fixtures as evidence. Do not infer reliance from the OpenAPI schema alone.

Present a table to the user before writing files:

| Operation | Request surface | Response surface | Status/behavior | Evidence |
|---|---|---|---|---|

For each row, name the code or test that proves the reliance. List unknowns as
gaps. Ask the user to confirm the table, the support level, and the provider
baseline (following **Provider baseline SHA**: explain why it matters, propose the
3.6 release base or the current provider `main` SHA, and show the SHA with its
source). If the consumer relies on several provider contracts, handle each
relationship separately.

### 3. Draft the reliance profile

Write `.rhoai/consumer-profile.yaml` in the consumer repository using the shape
in `$ACT/docs/onboarding/consumer-compat.md#3-write-the-reliance-profile`. Keep
the profile reviewable:

- stable, human-readable operation names;
- JSON pointers for response fields, including the response envelope;
- every relied-on status code and error shape;
- behavior that the schema cannot express;
- the test command and test paths that prove the profile;
- known gaps and exclusions.

The shared runner does not consume this file yet. Do not claim that it does, and
do not add a schema or runner dependency that the central repository has not
published.

### 4. Verify or add consumer contract tests

Use the consumer's existing test framework. Exercise the real consumer boundary,
not a hand-written HTTP call that bypasses the client. Validate the response
against the provider OpenAPI document where applicable, and assert the concrete
fields, statuses, and behavior in the profile independently of the schema.

Cover the cases the consumer actually handles, such as success, empty results,
not-found, validation errors, pagination, and authorization. Do not assert
provider internals or behavior the consumer does not use.

If the consumer already has a harness, use it. For the Dashboard Model Registry
package the command is:

```bash
pnpm --filter @odh-dashboard/model-registry test:contract
```

The reference tests and CI live in
`packages/model-registry/contract-tests/` and
`.github/workflows/model-registry-contract.yml` in `odh-dashboard`.

### 5. Run against a pinned provider

Check out the provider at the recorded full SHA and run the consumer tests
against that checkout. Record the provider SHA, test command, tool versions, and
result. If the harness supports overriding the OpenAPI or BFF path, run a second
time against the resolved provider `main` SHA as a drift signal and print that
SHA in the log. A drift failure is a finding to discuss with the provider owner,
not a reason to weaken the tests.

### 6. Prepare enforce-mode CI

Add or update a consumer workflow so it runs when consumer code, the reliance
profile, the tests, or a provider sync changes. Run it in enforce mode, so a failed
assertion against the pinned provider baseline fails the job; the drift run against
provider `main` stays informational and non-blocking. If the workflow calls a reusable
workflow from `api-contract-tests`, keep `mode: enforce` explicit (it is the default) and
pin it to `@latest` (a version tag, `stable`, or a
full commit SHA also work); never a branch.

Keep the consumer job cluster-free. Do not make a provider pull request run the
consumer's full end-to-end suite. Upload the test results as an artifact when the
repository already has an artifact convention.

### 7. Register and hand back

Prepare a summary for the working group:

- provider repository, contract ID, artifact path, and baseline SHA/digest, where
  the baseline came from (the 3.6 release base or current provider `main`), and
  that the user confirmed it;
- consumer repository, profile path, test command, and CI workflow;
- consumer and provider owners;
- support level and provider releases covered;
- known gaps, exclusions, and behavior not covered by tests.

Also tell the user what you changed, what you could not verify locally, and which
questions they answered. Offer to commit on a branch; do not push or open a pull
request unless asked.

## Out of scope

Creating or changing the provider descriptor, provider behavior tests, provider
baselines, or provider CI; implementing a shared consumer runner or profile
schema; and making the check a required branch protection are owner decisions
for later.
