# coverage module

Confirms that every API contract in a provider repository is registered, and
inventories a repository that has not been onboarded yet.

- `coverage` reads `.rhoai/contracts.yaml` and reports each OpenAPI document as
  a `contract`, a generator `source`, an `excluded` document with a recorded
  reason, or `UNCOVERED`. With `--crd-glob` it also requires every
  CustomResourceDefinition manifest to be matched by the glob the CRD check uses.
- It also lists per-contract baselines and warns when one is newer than, narrower than, or diverged from the default
  baseline (`--baseline-ref` or `spec.baseline.refs`), each of which loosens protection.
- `discover` needs no descriptor. It lists the documents, labels each `bundled`,
  `source`, or `standalone`, and suggests a `matchPath` and generator adapter. Each
  document also has a `dialect` (`openapi-3` or `swagger-2.0`). The suggested `matchPath`
  includes the document's base path (Swagger `basePath`, or the path of the OpenAPI 3
  `servers`), because that is how the checker matches paths. A document whose servers
  disagree on the path gets a `base_path_error` and a catch-all suggestion instead.

Discovery scans files git knows about (tracked, plus untracked and not
ignored) and recognizes an OpenAPI document by its top-level `openapi:` or
`swagger:` version key. Descriptor parsing uses the pinned `yq`, like the
OpenAPI check, so no extra dependency is added.

Exit codes for `coverage`: 0 complete, 1 gaps (0 with `--report-only`), 2
integrity error. See [`docs/onboarding`](../../docs/onboarding/README.md) and the
[runner README](../../cmd/contract-tests/README.md).

```bash
OASDIFF=/path/to/oasdiff YQ=/path/to/yq python3 -m pytest modules/coverage/tests -v
```
