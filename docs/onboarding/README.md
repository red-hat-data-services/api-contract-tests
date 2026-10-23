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
   exclusion with a reason. Fix gaps by registering the document or excluding
   it. Never by deleting the check.

6. **Add the workflow** in report mode, pinned to a full commit SHA of this
   repository. See [Adding CI](openapi-compat.md#5-add-ci).

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

The baseline is a full Git SHA of your own repository. Which commit to use is
a per-repository decision that the working group has not yet standardized; the
two candidates are the head of the last EA release and the head of the last
minor release. Whatever you choose:

- there should be no breaking API change within one major version, so moving
  the baseline forward is an exception that someone other than the author must
  approve;
- pass several `baseline-refs` when more than one supported release must stay
  compatible;
- never use a branch name, tag, or `HEAD`; the runner rejects them.

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
