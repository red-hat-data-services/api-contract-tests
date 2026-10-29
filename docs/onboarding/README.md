# Onboarding a repository

This is the end-to-end path for putting a provider repository under the shared
contract checks. The detailed guides are linked from each step. Rollout is
opt-in: nothing here is a promotion gate, and every check starts in report
mode.

## What you are adding

| Piece | Where it lives in your repository | Purpose |
|---|---|---|
| `ContractSet` descriptor | `.rhoai/contracts.yaml` | Names each API contract, how its candidate is generated, and the compatibility policy |
| Compatibility workflow | `.github/workflows/api-contract.yml` | Runs the OpenAPI (and CRD) checks on pull requests against a pinned baseline commit |
| Coverage check | same workflow | Fails (once enforced) when an OpenAPI document or CRD exists that the descriptor does not account for |

Your repository keeps its canonical OpenAPI documents and CRDs. This
repository supplies the runner, schemas, and reusable workflows.

## Checklist

1. **Inventory what you have.** From a checkout of this repository:

   ```bash
   python3 cmd/contract-tests/cli.py discover --repo /path/to/your/repo
   ```

   `discover` lists every OpenAPI document and CRD manifest, labels each one
   `bundled` (generated from a `src/` sibling), `source` (a bundle input), or
   `standalone` (hand-maintained), and suggests a `matchPath` and generator for
   each. These are hints, not decisions: you still choose the scope.

2. **Decide the scope of each contract.** For each API a consumer relies on,
   decide the `matchPath` that bounds it. A bundled document can carry several
   APIs (the Model Registry catalog bundle also carries plugin APIs that are
   deliberately out of scope for the Dashboard contract). Documents that are not
   provider contracts, such as an internal UI API, go in `spec.exclusions` with
   a reason a reviewer can read.

3. **Write `.rhoai/contracts.yaml`.** See the
   [OpenAPI guide](openapi-compat.md#2-write-a-contractset-descriptor). One
   descriptor can hold several contracts.

4. **Run the checks locally** with the pinned tools:
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

6. **Add the workflow** in report mode, pinned to a release of this
   repository, for example `@v0.1.0`. See [Adding CI](openapi-compat.md#5-add-ci)
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

## Choosing a baseline

The baseline is a full Git SHA of your own repository, and it defines what counts
as released: whatever exists there must stay compatible; whatever does not exist
there yet is new and may still change. Which commit to use (the last EA release or
the last minor release) is a per-repository decision that the working group has
not standardized. Whatever you choose:

- there should be no breaking API change within one major version, so moving
  the baseline forward is an exception that someone other than the author must
  approve;
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

## Moving from report to enforce

Stay in report mode until the findings have been reviewed and the provider and
its consumers agree on the policy. Then change `mode: report` to
`mode: enforce` on the compatibility job and, separately, on the coverage job.
Making a job required for merges is a branch-protection decision in your
repository, not something this repository does for you.

## Using an assistant

`.claude/skills/onboard-api-contracts/SKILL.md` encodes steps 1–6 for Claude
Code: it runs `discover`, drafts the descriptor, asks you the scoping and
baseline questions, runs the checks and `coverage`, and prepares the workflow.
It never opens a pull request or pushes without being asked.
