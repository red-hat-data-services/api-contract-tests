# Onboarding a repository

This repository has two onboarding paths. Choose the one that matches what your
repository owns:

| Your role | What you own | Start here |
|---|---|---|
| Provider | The canonical OpenAPI document or CRDs, provider behavior tests, and the `ContractSet` descriptor | [Provider checklist](#provider-checklist), then the [OpenAPI](openapi-compat.md) or [CRD](crd-compat.md) guide |
| Consumer | The reliance profile, consumer-side contract tests, and the CI job that runs them | [Consumer guide](consumer-compat.md) and [Consumer checklist](#consumer-checklist) |

Provider rollout is opt-in: nothing here is a promotion gate, and every check runs
in `mode: enforce`, the default, so a pull request that breaks a registered contract
fails the job. Consumer reliance checks are repository-local today; the
consumer guide records the profile and tests that the follow-on shared verifier
will adopt.

## What a provider adds

| Piece | Where it lives in your repository | Purpose |
|---|---|---|
| `ContractSet` descriptor | `.rhoai/contracts.yaml` | Names each API contract, how its candidate is generated, and the compatibility policy |
| Compatibility workflow | `.github/workflows/api-contract.yml` | Runs the OpenAPI (and CRD) checks on pull requests against a pinned baseline commit |
| Coverage check | same workflow | Fails (once enforced) when an OpenAPI document or CRD exists that the descriptor does not account for |

Your repository keeps its canonical OpenAPI documents and CRDs. This
repository supplies the runner, schemas, and reusable workflows.

## Provider checklist

1. **Inventory what you have.** From a checkout of this repository:

   ```bash
   python3 cmd/contract-tests/cli.py discover --repo /path/to/your/repo
   ```

   `discover` lists every OpenAPI document and CRD manifest, labels each one
   `bundled` (generated from a `src/` sibling), `source` (a bundle input), or
   `standalone` (hand-maintained), and suggests a `matchPath` and generator for
   each. These are hints, not decisions: you still choose the scope. A Swagger 2.0
   document is labeled `swagger-2.0` and is converted to OpenAPI 3 before it is
   compared; see [Swagger 2.0 documents](openapi-compat.md#swagger-20-documents).

2. **Decide the scope of each contract.** For each API a consumer relies on,
   decide the `matchPath` that bounds it. A bundled document can carry several
   APIs (the Model Registry catalog bundle also carries plugin APIs that are
   deliberately out of scope for the Dashboard contract). Documents that are not
   provider contracts, such as an internal UI API, go in `spec.exclusions` with
   a reason a reviewer can read.

3. **Write `.rhoai/contracts.yaml`.** See the
   [OpenAPI guide](openapi-compat.md#2-write-a-contractset-descriptor). One
   descriptor can hold several contracts.

4. **Run the checks locally** with the pinned tools, in enforce mode (the default), and the baseline
   you chose in [Choosing a baseline](#choosing-a-baseline):
   [OpenAPI](openapi-compat.md#4-run-it-locally) and, if you ship CRDs,
   [CRD](crd-compat.md#run-locally).

5. **Confirm nothing is left out:**

   ```bash
   python3 cmd/contract-tests/cli.py coverage --repo /path/to/your/repo \
     --crd-glob 'config/crd/**/*.yaml'      # only if you ship CRDs
   ```

   Every OpenAPI document must be a contract, a generator input, or an
   exclusion with a reason. It also lists per-contract baselines and flags any
   newer than the default. Fix gaps by registering the document or excluding
   it. Never by deleting the check.

6. **Add the workflow** in `mode: enforce`, pinned to a release of this
   repository, for example `@v0.1.0`, with `baseline-refs` set to the baseline SHA
   you chose. See [Adding CI](openapi-compat.md#5-add-ci)
   and [releases and pinning](../releasing.md) for the choices (version tag,
   `stable`, `latest` or a full commit SHA).

7. **Protect the configuration.** The descriptor, the workflow, and the
   baseline commit are what keep the check honest. Make them code-owned (for
   example a `CODEOWNERS` entry for `.rhoai/` and the workflow file) so that
   changing a baseline or a policy always needs an owner's review, ideally
   including someone from the contract working group rather than only the team
   that owns the component. A dedicated CI check that flags pull requests
   changing the baseline commit is planned and not yet available.

8. **Register the contract** with the working group
   ([step 6 of the OpenAPI guide](openapi-compat.md#6-register-the-contract-with-the-working-group)).

## Consumer checklist

1. **Identify the provider contract.** Record the provider repository, contract
   ID, declared artifact path, owner, support level, and an immutable full-SHA
   baseline, chosen as in [Choosing a baseline](#choosing-a-baseline) but for the
   provider repository. If the provider has not registered a contract, start with the
   [provider checklist](#provider-checklist) instead.

2. **Inventory the consumer's reliance.** Use code and tests to list the
   operations, parameters, response fields, status codes, error shapes, and
   behavior the consumer actually uses. Mark unknowns rather than guessing.

3. **Write the reliance profile.** Put `.rhoai/consumer-profile.yaml` in the
   consumer repository using the shape in the [consumer guide](consumer-compat.md#3-write-the-reliance-profile).
   The shared runner does not consume it yet; it is the reviewable record for
   the follow-on verifier.

4. **Add consumer contract tests.** Exercise the real consumer boundary, validate
   the response against the provider OpenAPI document, and assert the fields,
   statuses, and behavior named in the profile. See the
   [Dashboard reference implementation](consumer-compat.md#dashboard-reference-implementation).

5. **Run against a pinned provider.** Record the provider SHA, test command, tool
   versions, and result. Run against provider `main` as a separate drift signal;
   do not record the name `main` as the baseline.

6. **Add enforce-mode CI.** Run the consumer tests on consumer changes and
   provider syncs. Pin any reusable workflow to a release tag or full SHA, never
   a branch. See [consumer CI](consumer-compat.md#6-add-ci-in-enforce-mode).

7. **Register the relationship.** Provide the provider contract, consumer
   profile, test command, CI workflow, owners, support level, and known gaps to
   the working group.

## Choosing a baseline

The baseline is the most important value in the configuration. It is a full Git
SHA of your own repository, and it defines what counts as released: whatever
exists there must stay compatible; whatever does not exist there yet is new and
may still change.

**Why the choice matters.**

- A baseline that is too new (for example a commit after a breaking change)
  silently accepts breaks to APIs that already shipped: the check passes and
  consumers break later.
- A baseline that is too old, or from another release line, makes every
  intentional change since then look like a break. The check fails on every pull
  request and teams stop trusting it.
- The baseline is a promise. Moving it forward is an exception that someone other
  than the author must approve, so a wrong choice is expensive to undo.

**Which commit to use.** Pick the first that applies:

1. The commit the RHOAI 3.6 release was based on. The downstream repositories
   (`red-hat-data-services/*`) have no 3.6 tags, only a `rhoai-3.6` branch, and that
   branch's head is a downstream-only commit: in the 68 repositories where the two
   histories can be compared, it does not exist in the `opendatahub-io` repository
   the check runs in, so it cannot be a baseline there. Use the newest commit both
   repositories share (the merge base of `rhoai-3.6` and the default branch).
   [`release_baseline.py`](../../cmd/release-baseline/README.md) computes it:
   `lookup --release 3.6 --repo <org>/<repo>` reads the checked-in
   [`data/release-baselines/3.6.json`](../../data/release-baselines/3.6.json)
   without network access, and `resolve` recomputes it from GitHub.
2. Otherwise, when the tool reports no baseline (for example a repository with no
   downstream counterpart or no shared history), the SHA that `main` (the default
   branch) resolves to today:
   `git rev-parse refs/remotes/<trusted-remote>/main`. Everything on `main` is then
   treated as released and protected from now on, earlier releases are not
   compared, and anything already broken on `main` is accepted.

This is the recommended default, not a rule: a repository that must protect a
different supported release can use it, and should record the reason. Whichever you
choose:

- record the SHA's source (for example "merge base of rhoai-3.6 and main, from
  release_baseline.py on <date>", or "`main` on <date>") in a comment next to the
  setting, so a reviewer can verify it;
- use the same SHA everywhere the repository default is set: the workflow's
  `baseline-refs`, `--baseline-ref` in local runs, and `spec.baseline.refs` if the
  descriptor carries one;
- there should be no breaking API change within one major version, which is why
  moving the baseline forward needs someone other than the author to approve it;
- pass several baselines when more than one supported release must stay
  compatible; each contract is compared only against the baselines that contain it;
- never use a branch name, tag, or `HEAD`; the runner rejects them.

**New APIs.** If a contract's artifact does not exist at a baseline, the contract
is reported as `new` for that baseline: nothing is compared and the check does not
fail, even when enforced. It is protected as soon as you adopt a baseline that
contains it, for example when the release that first ships it is cut. The same holds
for CRDs when the baseline has no CRDs at all. Two cases stay errors because they
would otherwise hide a configuration mistake: a baseline that has the artifact but
no paths under `matchPath` (wrong version), and an artifact that exists at the
baseline under a different name (a move or rename; the error names the old file).
With a catch-all `matchPath` the check cannot tell a rename from a new API, so it
fails closed whenever another OpenAPI document exists at the baseline.

**Per-contract baselines.** The repository default applies to every contract. To
give one contract its own baseline, for example to scope an intentional break to a
single API without moving everyone else's baseline, add a `baseline` block to that
contract in `.rhoai/contracts.yaml` with a `reason`:

```yaml
contracts:
  - id: your-service-v1
    baseline:
      refs: ["<full-git-sha>"]
      reason: Intentional removal of /items/legacy agreed with the Dashboard team (RHOAIENG-1234)
      # artifact: old/path/openapi.yaml   # only if the file was later moved
```

A contract's own baseline is a promise: the artifact must exist at those commits
or the check fails. Precedence is the contract's `baseline`, then the workflow or
runner `--baseline-ref` (the repository default), then `spec.baseline.refs` in the
descriptor. `coverage` lists every per-contract baseline and warns when one is
newer than the default, keeps fewer of the default baselines (for example
`[A]` when the default is `[A, B]`), or cannot be ordered against it, because each
loosens protection for that contract. Pass the default as `baseline-refs` to the
coverage workflow (and use a full checkout) so this works in CI.

## Enforce mode

Every job runs in `mode: enforce`: a pull request that breaks a registered contract
fails the job, on the compatibility job and on the coverage job. Enforce is the default
for the OpenAPI, CRD, and coverage workflows and for the CLI (`check`, `crd-check`,
and `coverage`). Report mode (`mode: report`, or `--report-only` on the command line)
records findings without failing and is not recommended.

The examples still set `mode: enforce` explicitly. Keep it: it documents the intent
in your workflow, and releases up to and including `v1.0.0` defaulted `mode` to
`report`, so a caller pinned to one of them that omits `mode` gets a job that stays
green on a breaking change.

If the first run fails because the repository already broke its baseline, that is a
finding, not a reason to use report mode. Fix the break or, for an OpenAPI contract
whose break was intentional and agreed, record a per-contract `baseline` override
with a `reason` (see [Choosing a baseline](#choosing-a-baseline)). Making a job
required for merges is a branch-protection decision in your repository, not
something this repository does for you.

## Using an assistant

Two assistant skills encode these paths:

- `.claude/skills/onboard-api-contracts/SKILL.md` covers the provider path. It
  runs `discover`, drafts the descriptor, explains why the baseline SHA matters and
  proposes one (the 3.6 release base, or the current `main` SHA), asks the scoping
  questions, runs the checks and `coverage`, and prepares the workflow.
- `.claude/skills/onboard-consumer-contracts/SKILL.md` covers the consumer path.
  It inventories reliance from code and tests, proposes the provider baseline the
  same way, drafts the profile, verifies the consumer tests, and prepares
  enforce-mode CI.

Both skills stop before pushing or opening a pull request unless you ask.
