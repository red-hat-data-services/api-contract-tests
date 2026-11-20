# manifest-compat

Static Kubernetes manifest compatibility for provider pull requests. Renders
the provider's declared installation sources — raw YAML, a local Helm chart,
or a local Kustomize overlay — with pinned tools and compares the rendered
objects against immutable Git baselines identified by full commit SHAs.

The check detects declared in-place update compatibility: immutable fields,
conditional update restrictions, transition rules, and manifest-level
relationships (selectors, required references, protected labels and
resources). It does **not** validate against a cluster, execute admission
webhooks, or read live state; see
[docs/research/manifest-compatibility.md](../../docs/research/manifest-compatibility.md)
for the full scope and the follow-on roadmap.

## Source of truth

- [`policy.json`](policy.json) — the complete finding-ID registry: stable
  IDs, kinds, field paths, strategies, guards,
  severity/evidence, and waiver eligibility. Every immutable-field rule cites
  the exact upstream Kubernetes commit it was audited from (minors 1.35, 1.36,
  1.37; audited patches v1.35.10, v1.36.6, v1.37.2). This file is the only
  field catalogue; no other list is maintained. The check list below is a
  summary of it — when they disagree, the registry wins.
- [`schemas/manifest-set.schema.json`](../../schemas/manifest-set.schema.json)
  — the `ManifestSet` descriptor schema.
- [`docs/onboarding/manifest-compat.md`](../../docs/onboarding/manifest-compat.md)
  — the provider onboarding guide (local runs, baselines, CI, report
  semantics).

## Implemented checks

Rule IDs are stable and match `policy.json`; every field rule names the
upstream Kubernetes validator it was audited from. Findings carry a severity
of error, warn, or info; error findings block in enforce mode.

### Workloads

- `spec.selector` is immutable for `Deployment`, `DaemonSet`, `ReplicaSet`,
  `StatefulSet`, and `Job` (`*-selector-immutable`).
- `StatefulSet`: `spec.serviceName`, `spec.podManagementPolicy`, and
  `spec.volumeClaimTemplates` are immutable; the rest of `spec` is an
  allowlist — only pod-template fields may change
  (`statefulset-service-name-immutable`,
  `statefulset-pod-management-policy-immutable`,
  `statefulset-volume-claim-templates-immutable`,
  `statefulset-spec-field-forbidden`).
- `ControllerRevision`: `data` is immutable
  (`controllerrevision-data-immutable`).
- `Job`: `completionMode`, `podFailurePolicy`, `backoffLimitPerIndex`,
  `managedBy`, and `successPolicy` are immutable; the pod template is frozen;
  `completions`/`parallelism` follow the completions rule; scheduling-field
  updates are warned as risk; `CronJob` job-template mutability is recorded as
  a coverage note (`job-completion-mode-immutable`,
  `job-pod-failure-policy-immutable`, `job-backoff-limit-per-index-immutable`,
  `job-managed-by-immutable`, `job-success-policy-immutable`,
  `job-completions-rule`, `job-template-immutable`, `job-scheduling-update`,
  `cronjob-jobtemplate-mutable`).
- `Pod` updates are restricted to the allowlisted in-place fields
  (`pod-update-restricted`).

### Storage

- `StorageClass`: `provisioner`, `parameters`, `reclaimPolicy`, and
  `volumeBindingMode` are immutable (`storageclass-*-immutable`).
- `PersistentVolume`: the volume source and `volumeMode` are immutable;
  node-affinity and `volumeAttributesClassName` changes are warned as risk
  (`pv-volume-source-immutable`, `pv-volume-mode-immutable`,
  `pv-node-affinity-risk`, `pv-volume-attributes-class-name-risk`).
- `PersistentVolumeClaim`: `accessModes`, `selector`, `dataSource`,
  `dataSourceRef`, and `volumeMode` are immutable; `volumeName` is
  one-time-initialized from unset; `storageClassName` permits exactly the two
  source-verified migrations — unset to set (the audited API accepts
  nil→`""`, so this is not documented as a universal rejection) and the beta
  annotation to an equal field value — and errors on everything else;
  storage expansion and `volumeAttributesClassName` changes are warned as
  risk (`pvc-access-modes-immutable`, `pvc-selector-immutable`,
  `pvc-data-source-immutable`, `pvc-data-source-ref-immutable`,
  `pvc-volume-mode-immutable`, `pvc-volume-name-immutable`,
  `pvc-storage-class-name-immutable`, `pvc-storage-resize-risk`,
  `pvc-volume-attributes-class-name-risk`).

### Networking — Service

- `clusterIPs` and `ipFamilies` are transition-validated
  (`service-cluster-ips-transition`, `service-ip-families-transition`).
- `loadBalancerClass` and `healthCheckNodePort` are immutable while the
  Service type is LoadBalancer (`service-load-balancer-class-immutable`,
  `service-health-check-node-port-immutable`).

### Configuration

- `ConfigMap` and `Secret` data is immutable only when the baseline sets
  `immutable: true` — the gate is read from the baseline, never the candidate
  (`configmap-immutable-data`, `secret-immutable-data`).
- `Secret.type` and `ResourceQuota.scopes` are immutable
  (`secret-type-immutable`, `resourcequota-scopes-immutable`).

### RBAC

- `roleRef` is immutable for `RoleBinding` and `ClusterRoleBinding`
  (`rolebinding-roleref-immutable`, `clusterrolebinding-roleref-immutable`).

### Node and scheduling

- `RuntimeClass.handler` is immutable (`runtimeclass-handler-immutable`).
- `PriorityClass.value` and `preemptionPolicy` are immutable
  (`priorityclass-value-immutable`, `priorityclass-preemption-policy-immutable`).

### Resource (DRA) and migration

- `ResourceClaim` and `ResourceClaimTemplate` specs are wholly immutable;
  `ResourceSlice` driver, pool name, and node name are immutable;
  `StorageVersionMigration` spec is wholly immutable
  (`resourceclaim-spec-immutable`, `resourceclaimtemplate-spec-immutable`,
  `resourceslice-driver-immutable`, `resourceslice-pool-name-immutable`,
  `resourceslice-node-name-immutable`, `storageversionmigration-spec-immutable`).

### Certificates

- `CertificateSigningRequest` spec updates the API server silently ignores —
  flagged rather than dropped (`csr-spec-ignored-update`).
- `ClusterTrustBundle.signerName` is immutable; `PodCertificateRequest.spec`
  is wholly immutable (`clustertrustbundle-signer-name-immutable`,
  `podcertificaterequest-spec-immutable`).

### Cross-resource relationships

- Workload selector/template consistency is validated at render time
  (`selector-template-mismatch`).
- A probe handler port written as a name must resolve to a named container
  port on the same container; the API server accepts unresolved names but
  the kubelet cannot run the probe, so the pod never becomes Ready
  (`probe-port-unresolved`).
- Selector target loss and mutable-selector changes are detected for Service,
  PodDisruptionBudget, and NetworkPolicy (`selector-target-lost`,
  `mutable-selector-changed`).
- Required references that disappear and external references that cannot be
  verified are reported (`required-reference-lost`,
  `external-reference-unverified`).
- Protected labels and protected resources named by the descriptor are
  contract-level errors (`protected-label-changed`,
  `protected-resource-removed`).

### Drift and removal

- Label removal, label value changes, and resource removal
  (`label-removed`, `label-value-changed`, `resource-removed`).

### Coverage and diagnostics

- Conditional-update risks, partially covered rules, and extrapolated patch
  coverage are surfaced (`conditional-update-risk`, `rule-coverage-partial`,
  `patch-coverage-extrapolated`); exception waivers that no longer match a
  finding are reported as stale (`stale-exception`).

## Layout

| File | Purpose |
| --- | --- |
| `checker.py` | Run orchestration, report assembly, CLI entry (`manifest-prepare`, `manifest-check` via `cmd/contract-tests/cli.py`) |
| `config.py` | `ManifestSet` loading and validation |
| `renderers.py` | Pinned offline rendering (`go run` Helm/Kustomize), snapshots, tool-cache preparation |
| `normalizer.py` | Rendered-output parsing and structural normalization |
| `policies_*.py` | Field and transition rules per API family (workloads, storage/Service, resources) |
| `relationships.py` | Selector and reference graph rules |
| `policy.json` | The finding-ID registry and audited-version metadata |

## Rendering model

Renders run through `go run helm.sh/helm/v4/cmd/helm@v4.3.1` and
`go run sigs.k8s.io/kustomize/kustomize/v5@v5.8.3` — never installed
binaries. `manifest-prepare` is the only step that needs the network: it
fetches and checksum-verifies the exact modules into a caller-selected cache
outside the provider checkout and proves the cache renders offline.
`manifest-check` then renders with the module proxy pointed at the prepared
cache itself and fails closed on a missing or unusable cache. Renderer
subprocesses run in isolated working directories with allowlisted
environments, and charts and overlays may reference only local paths inside
the repository — but this is tool-configuration isolation, not OS-enforced
network or filesystem sandboxing. Helm templates may
not use `lookup` or `tpl`; every render runs twice and must be
byte-deterministic. Helm hook objects and CustomResourceDefinitions are
skipped and recorded (CRDs belong to the CRD compatibility check).

## Tests

```bash
# Offline unit/regression suites (no Go toolchain or cache needed).
python3 -m pytest modules/manifest-compat/tests -v

# Real-renderer integration group: needs Go >= 1.27 on PATH and a cache
# prepared by manifest-prepare; fails (never skips) when either is missing.
python3 cmd/contract-tests/cli.py manifest-prepare --repo . \
  --manifest-set selftest/manifests.yaml --tool-cache-dir /tmp/manifest-tools
MANIFEST_RENDERERS_CACHE=/tmp/manifest-tools \
  python3 -m pytest modules/manifest-compat/tests/test_renderer_integration.py \
  -m manifest_renderers -v
```

The `manifest_renderers` marker is registered in the repository-root
`pytest.ini` and excluded from ordinary runs; CI runs it in a dedicated job.
