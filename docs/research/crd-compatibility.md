# CRD compatibility research

## Goal

Define a repeatable, provider-owned CRD compatibility check that runs in
provider CI and catches breaking changes before integration or release testing.

## Scope

### In scope

- CRDs consumed by Dashboard, operators, or other RHOAI components.
- Schema compatibility.
- Served and storage API versions.
- Conversion and deprecation paths.
- Defaulting and validation changes.
- Status fields and conditions consumers rely on.
- Upgrade behavior for existing stored objects.

### Out of scope

- Replacing full cluster end-to-end tests.
- Solving every operator at once.
- General Kubernetes conformance.

## Compatibility dimensions

| Dimension | What must be checked |
|---|---|
| Schema | Removed or renamed fields, type changes, newly required fields, enum narrowing, default changes, validation tightening |
| API versions | Served-version removal, storage-version changes, deprecation policy, conversion path |
| Semantics | Reconciliation behavior, status conditions, readiness, observed fields consumers depend on |
| Upgrade | Existing stored objects remain valid and convertible; N-1 fixtures continue to work |
| Consumer reliance | Explicit fields, conditions, and statuses consumers read or write |

## Candidate approaches

- Regenerate CRDs with `controller-gen` and compare with committed YAML.
- Evaluate OLM's CRD upgrade validation and determine which findings can move earlier into provider CI.
- Evaluate schema validation tools such as `kubeconform`.
- Use bounded `envtest` scenarios for API-server, admission, conversion, and reconciliation behavior.
- Evaluate semantic CRD differ tools; do not assume a specific tool name until verified.
- Consider `go-apidiff` for Go types consumed as libraries.

## Evaluation criteria

A candidate approach is useful only if it:

- detects removed or narrowed consumer reliance;
- detects incompatible schema, version, conversion, and upgrade changes;
- runs in provider CI in under five minutes where possible;
- does not require a full cluster for the common pull-request case;
- can be pinned to an exact version or commit SHA;
- emits structured findings with file, path, field, and severity;
- integrates with this repository's shared runner and report format.

## Proposed first CRD pilot

Select one high-impact CRD from `opendatahub-operator`, likely one consumed by
Dashboard. The pilot should:

1. Regenerate the CRD from pull-request source.
2. Compare it with immutable rolling and supported-release baselines.
3. Load a consumer profile listing relied-on fields and conditions.
4. Validate representative objects against the candidate schema.
5. Exercise conversion and upgrade behavior with bounded `envtest` fixtures.
6. Run initially in report-only mode.
7. Seed one compatible change, one breaking schema change, and one conversion or upgrade failure.

## Next steps

1. Inventory CRDs consumed by Dashboard and the follow-on operators.
2. Choose the first CRD pilot with the provider owner.
3. Define the consumer reliance profile.
4. Evaluate candidate tools against seeded compatible and breaking changes.
5. Implement a report-only prototype in this repository.
