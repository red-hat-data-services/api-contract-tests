"""Candidate-generation adapters for the openapi-compat module.

Turning a provider's declared OpenAPI source into the exact candidate
document compared against baselines is inherently provider-specific: every
repository bundles, generates, or hand-maintains its spec differently. Only
a small, reviewed set of adapters is registered here. A descriptor naming an
unregistered adapter fails closed rather than falling back to executing
arbitrary generation logic.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class AdapterError(Exception):
    """The candidate could not be generated from the declared source."""


def _run(args, *, cwd, env=None):
    try:
        result = subprocess.run(
            [str(arg) for arg in args], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError(f"Cannot run {args[0]}: {error}") from error
    if result.returncode:
        raise AdapterError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return result


def yq_merge(repo, spec, yq, destination):
    """Bundle an OpenAPI document from declared source globs using a
    provider-owned shell script and yq (the pattern used by
    scripts/merge_catalog_specs.sh in opendatahub-io/model-registry).

    Generation always happens in a private temporary workspace; the
    developer's working tree and any already-committed artifact are never
    touched or rewritten by this adapter.
    """
    required = {"script", "sourceDir", "sourceGlobs", "entrypointArg", "output"}
    missing = required - spec.keys()
    if missing:
        raise AdapterError(f"openapi.bundle.yq-merge generator missing fields: {sorted(missing)}")
    with tempfile.TemporaryDirectory(prefix="contract-generation-") as temporary:
        workspace = Path(temporary)
        script_rel = Path(spec["script"])
        (workspace / script_rel.parent).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / script_rel, workspace / script_rel)

        source_dir = Path(spec["sourceDir"])
        source = repo / source_dir
        target = workspace / source_dir
        inputs = []
        for pattern in spec["sourceGlobs"]:
            matches = sorted(source.glob(pattern))
            if not matches:
                raise AdapterError(f"No source files matched {pattern!r} under {source_dir}")
            inputs.extend(matches)
        for path in inputs:
            relative = path.relative_to(source)
            (target / relative.parent).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target / relative)

        env_var = spec.get("yqEnvVar", "YQ")
        environment = dict(os.environ, **{env_var: str(yq)})
        _run(["bash", str(script_rel), spec["entrypointArg"]], cwd=workspace, env=environment)

        generated = workspace / spec["output"]
        if not generated.is_file():
            raise AdapterError(f"Generator did not produce declared output {spec['output']}")
        shutil.copyfile(generated, destination)


def static(repo, spec, yq, destination):
    """No generation step: the declared source is already the contract-first
    candidate (e.g. a hand-maintained OpenAPI document with no bundler or
    code-generation step). Still copied through the same path as every other
    adapter so the rest of the pipeline treats all contract types alike.
    """
    if "source" not in spec:
        raise AdapterError("openapi.static generator missing field: source")
    source = repo / spec["source"]
    if not source.is_file():
        raise AdapterError(f"Declared static source {spec['source']} does not exist")
    shutil.copyfile(source, destination)


REGISTRY = {
    "openapi.bundle.yq-merge": yq_merge,
    "openapi.static": static,
}


def generate(name, repo, spec, yq, destination):
    adapter = REGISTRY.get(name)
    if adapter is None:
        raise AdapterError(f"Unregistered generator adapter {name!r}; allowed: {sorted(REGISTRY)}")
    adapter(repo, spec, yq, destination)
