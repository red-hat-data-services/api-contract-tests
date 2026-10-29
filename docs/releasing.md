# Releases and pinning

Provider repositories call the reusable workflows in `.github/workflows/` from
their own CI. This page explains how a caller pins those workflows and how this
repository cuts the releases they pin to.

## Pinning the reusable workflows

Every provider workflow checks how it was called (`require-pinned-ref` in
`cmd/workflow-steps/steps.py`) and fails unless the ref after `@` is one of:

| Ref | Example | What runs | Upgrading |
|---|---|---|---|
| Version tag | `@v0.1.0` | Exactly that release; version tags are never moved | Change the tag in your workflow |
| `stable` | `@stable` | The release the working group recommends; moves when a release is promoted | Automatic |
| `latest` | `@latest` | The newest release; moves with every release | Automatic |
| Full commit SHA | `@<40-character sha>` | Exactly that commit | Change the SHA |

Branches (`main`, `refs/heads/...`), short SHAs, and any other tag name
(`v1`, `v1.2`, `v1.2.3-rc.1`, `release`) are rejected. For a tag, the job log
records the commit it resolved to, so every run can be traced to the exact code
it used.

Which one to use:

- **Version tag**: the default. One readable string to change when you upgrade,
  and nothing changes under you between upgrades. Use it once a check is
  enforced.
- **`stable`**: no upgrades to make. Your check can start reporting new findings
  after a promotion without a change in your repository, so it suits report mode
  better than an enforced gate.
- **`latest`**: for trying a release before it reaches `stable`. Not for gates.
- **Full commit SHA**: when you need the strongest guarantee, for example a
  repository that requires SHA pins for every action. Add the release as a comment
  (`@<sha> # v0.1.0`) so readers and update bots can see which release it is.

Dependabot (`package-ecosystem: github-actions`) and Renovate can open upgrade
pull requests for version tags and for SHA pins that carry a version comment.

A change to the pinned ref changes what the check protects, so keep the workflow
file code-owned (see the [onboarding checklist](onboarding/README.md#checklist),
step 7).

## Cutting a release

Releases follow [semantic versioning](https://semver.org/). Before 1.0.0, a minor
version may change workflow inputs, the descriptor schema, or the findings a
check reports; a patch version only fixes bugs. Describe anything a caller must
change in the release notes.

1. Make sure CI and the workflow self-test passed on the `main` commit you are
   releasing.
2. Create an annotated version tag on that commit and push it:

   ```bash
   git tag -a v0.1.0 <full-sha> -m "v0.1.0"
   git push origin v0.1.0
   ```

3. Publish release notes for the tag:

   ```bash
   gh release create v0.1.0 --verify-tag --title v0.1.0 --notes-file <notes.md>
   ```

4. Move `latest` to the new release:

   ```bash
   git tag -f latest 'v0.1.0^{commit}'
   git push -f origin latest
   ```

5. Move `stable` the same way once the release has run without problems in at
   least one onboarded repository, and say so in the working group channel.
   `stable` never moves backwards except to roll back a bad promotion.

Never move or delete a version tag. If a release is broken, publish a new patch
version and, if needed, move `stable` back to the last good release.

## Protecting the tags

Because callers can pin to tags, the tags are part of the security of every
onboarded repository: whoever can move a tag decides what code runs in their CI.
Ask an organization admin to add tag rulesets to this repository:

- `refs/tags/v*`: block updates and deletions, so version tags cannot move.
- `refs/tags/stable` and `refs/tags/latest`: restrict creation, updates and
  deletions to the repository maintainers, so only a release can move them.
- Optionally, block branches named like a release tag (`v*`, `stable`, `latest`)
  so a bare name in a caller's `uses:` can never mean a branch.

The workflows cannot verify these rulesets at run time (the caller's token cannot
read them), so the protection depends on keeping them in place.
