"""Generic OpenAPI compatibility engine (the openapi-compat module).

Loads a provider's ContractSet descriptor (see ../../schemas/contract-set.schema.json),
generates a fresh candidate using the descriptor's declared generator adapter
(adapters.py), resolves every baseline by exact Git SHA, and runs pinned
oasdiff checks scoped to the selected contract.

This module is provider-agnostic: no Model Catalog-specific paths, tool
versions, or bundling scripts live here. All of that comes from the
provider's own .rhoai/contracts.yaml.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_adapters():
    spec = importlib.util.spec_from_file_location("openapi_compat_adapters", _HERE / "adapters.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapters = _load_adapters()


class CheckError(Exception):
    """The comparison could not be completed reliably."""


def command(args, *, cwd, env=None, check=True):
    try:
        result = subprocess.run(
            [str(arg) for arg in args], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"Cannot run {args[0]}: {error}") from error
    if check and result.returncode:
        raise CheckError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return result


def tool_path(name, explicit, repo):
    if explicit:
        candidate = shutil.which(explicit) or explicit
    elif (repo / "bin" / name).is_file():
        candidate = repo / "bin" / name
    else:
        candidate = shutil.which(name)
    if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise CheckError(f"Missing executable {name}; pin it under bin/ or pass an explicit path")
    return Path(candidate).resolve()


def verify_oasdiff(binary, version, cwd):
    output = command([binary, "--version"], cwd=cwd).stdout.strip()
    if output in (f"oasdiff version {version}", f"oasdiff version v{version}"):
        return output
    # go install leaves build.Version as 'main'. Check its embedded module
    # version instead of accepting an unversioned development build.
    build_info = command(["go", "version", "-m", binary], cwd=cwd).stdout
    for line in build_info.splitlines():
        fields = line.split()
        if fields[:3] == ["mod", "github.com/oasdiff/oasdiff", f"v{version}"]:
            return f"{output}; module v{version}"
    raise CheckError(f"Expected oasdiff v{version}, got {output!r}")


def verify_yq(binary, version, cwd):
    output = command([binary, "--version"], cwd=cwd).stdout.strip()
    if re.search(rf"(?:^|\s)version v{re.escape(version)}(?:\s|$)", output):
        return output
    build_info = command(["go", "version", "-m", binary], cwd=cwd).stdout
    for line in build_info.splitlines():
        fields = line.split()
        if fields[:3] == ["mod", "github.com/mikefarah/yq/v4", f"v{version}"]:
            return f"{output}; module v{version}"
    raise CheckError(f"Expected yq v{version}, got {output!r}")


def validate_baseline_ref(ref):
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref) is None:
        raise CheckError(
            f"Baseline revision {ref!r} must be a full 40- or 64-character Git SHA; "
            "mutable refs such as main, HEAD, tags, and branches are not accepted"
        )


def resolve_commit(repo, ref):
    result = command(
        ["git", "--no-replace-objects", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repo, check=False,
    )
    sha = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise CheckError(f"Cannot resolve baseline revision {ref!r}; fetch the trusted revision first")
    return sha


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def spec_has_paths(yq, path, cwd, pattern):
    parsed = json.loads(command([yq, "eval", "-o=json", ".", str(path)], cwd=cwd).stdout)
    paths = parsed.get("paths") if isinstance(parsed, dict) else None
    return isinstance(paths, dict) and any(
        isinstance(name, str) and re.match(pattern, name) for name in paths
    )


def findings(result, operation):
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CheckError(f"oasdiff {operation} returned invalid JSON: {result.stderr or result.stdout}") from error
    if parsed is None:
        parsed = []
    if not isinstance(parsed, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        or not isinstance(item.get("text"), str) or not isinstance(item.get("level"), int)
        for item in parsed
    ):
        raise CheckError(f"oasdiff {operation} returned malformed findings")
    if result.returncode not in (0, 1) or (result.returncode == 1 and not parsed):
        raise CheckError(f"oasdiff {operation} failed: {result.stderr or result.stdout}")
    return parsed


def validate_spec(binary, policy, path, cwd):
    result = command(
        [binary, "validate", "--config", policy, "--allow-external-refs=false",
         "--fail-on", "ERR", "--format", "json", path], cwd=cwd, check=False,
    )
    records = findings(result, "validate")
    if result.returncode:
        raise CheckError(f"Invalid OpenAPI contract {Path(path).name}: {json.dumps(records)}")
    return records


def load_descriptor(path, yq, cwd):
    if not Path(path).is_file():
        raise CheckError(f"Descriptor not found: {path}")
    raw = command([yq, "eval", "-o=json", ".", str(path)], cwd=cwd).stdout
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CheckError(f"Cannot parse descriptor {path}: {error}") from error
    if not isinstance(data, dict) or data.get("kind") != "ContractSet":
        raise CheckError(f"{path} is not a ContractSet descriptor (missing kind: ContractSet)")
    return data


def select_contract(descriptor, contract_id):
    contracts = descriptor.get("spec", {}).get("contracts", [])
    if not isinstance(contracts, list) or not contracts:
        raise CheckError("Descriptor declares no contracts")
    if contract_id is None:
        if len(contracts) != 1:
            raise CheckError("Descriptor registers multiple contracts; --contract-id is required")
        return contracts[0]
    matches = [c for c in contracts if isinstance(c, dict) and c.get("id") == contract_id]
    if len(matches) != 1:
        raise CheckError(f"Contract id {contract_id!r} not found or not unique in descriptor")
    return matches[0]


def contract_baselines(args, descriptor, contract):
    """Resolve which baselines a contract is compared against, and where they came from.

    Precedence: the contract's own ``baseline`` block, then ``--baseline-ref`` (the
    repository default, normally supplied by the workflow), then ``spec.baseline``
    in the descriptor. A contract's own baseline is an explicit promise that the
    artifact exists there; the repository default is not.
    """
    contract_id = contract.get("id")
    block = contract.get("baseline")
    if block is not None:
        refs = block.get("refs") if isinstance(block, dict) else None
        reason = block.get("reason") if isinstance(block, dict) else None
        artifact = block.get("artifact") if isinstance(block, dict) else None
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
            raise CheckError(f"Contract {contract_id!r}: baseline.refs must be a non-empty list of full Git SHAs")
        if not isinstance(reason, str) or not reason.strip():
            raise CheckError(
                f"Contract {contract_id!r}: baseline needs a 'reason'; a per-contract baseline "
                "without a recorded justification is not accepted"
            )
        if artifact is not None and (not isinstance(artifact, str) or not artifact):
            raise CheckError(f"Contract {contract_id!r}: baseline.artifact must be a repo-relative path")
        return [(ref, "contract") for ref in refs], artifact, True
    if args.baseline_ref:
        return [(ref, "default") for ref in args.baseline_ref], None, False
    default = descriptor.get("spec", {}).get("baseline")
    refs = default.get("refs") if isinstance(default, dict) else None
    if isinstance(refs, list) and refs and all(isinstance(ref, str) for ref in refs):
        return [(ref, "default") for ref in refs], None, False
    raise CheckError(
        f"Contract {contract_id!r} has no baseline: pass --baseline-ref, or declare "
        "spec.baseline.refs or contract baseline.refs in the descriptor"
    )


def exists_at(repo, sha, relative):
    result = command(["git", "--no-replace-objects", "cat-file", "-e", f"{sha}:{relative}"], cwd=repo, check=False)
    return result.returncode == 0


def find_moved_artifact(repo, sha, match_path, yq, workdir, skip):
    """Find an OpenAPI document at the baseline that may be this contract's earlier self.

    Used when the declared artifact is absent: if the API existed under another
    name, calling it "new" would silently drop its protection.

    Returns ``(path, catch_all)``. With a specific matchPath, a document counts only
    if it already serves paths matching it. A catch-all matchPath cannot tell one API
    from another, so any other OpenAPI document at the baseline is returned and the
    caller must fail closed rather than grant the "new" exemption.
    """
    result = command(
        ["git", "--no-replace-objects", "grep", "-l", "-I", "-E",
         "-e", r"^(openapi|swagger)[[:space:]]*:", "-e", r'"(openapi|swagger)"[[:space:]]*:',
         sha, "--", "*.yaml", "*.yml", "*.json"],
        cwd=repo, check=False,
    )
    catch_all = match_path in (None, "", ".*")
    prefix = f"{sha}:"
    for line in result.stdout.splitlines():
        relative = line[len(prefix):] if line.startswith(prefix) else line
        if relative == skip:
            continue
        if catch_all:
            return relative, True
        probe = Path(workdir) / "moved-probe.yaml"
        raw = command(["git", "--no-replace-objects", "show", f"{sha}:{relative}"], cwd=repo, check=False)
        if raw.returncode:
            continue
        probe.write_text(raw.stdout, encoding="utf-8")
        try:
            if spec_has_paths(yq, probe, workdir, match_path):
                return relative, False
        except CheckError:
            continue
    return None, False


def check_compatibility(args, output, report):
    repo = args.repo.resolve()
    oasdiff = tool_path("oasdiff", args.oasdiff, repo)
    yq = tool_path("yq", args.yq, repo)
    for ref in args.baseline_ref or []:
        validate_baseline_ref(ref)

    descriptor = load_descriptor(repo / args.contract_set, yq, repo)
    contract = select_contract(descriptor, args.contract_id)
    contract_id = contract.get("id")
    if contract.get("type") != "openapi":
        raise CheckError(f"Contract {contract_id!r} is type {contract.get('type')!r}; openapi-compat only handles 'openapi'")

    declared_artifact = contract.get("declaredArtifact")
    match_path = contract.get("matchPath", ".*")
    policy = contract.get("policy", {})
    oasdiff_version = policy.get("oasdiffVersion")
    yq_version = policy.get("yqVersion")
    if not declared_artifact or not oasdiff_version or not yq_version:
        raise CheckError(
            f"Contract {contract_id!r} must declare declaredArtifact, "
            "policy.oasdiffVersion, and policy.yqVersion"
        )
    severity_overrides = policy.get("severityOverrides", {})
    flatten_allof = policy.get("flattenAllOf", True)

    report["contract_id"] = contract_id
    report["tool_build"] = {
        "oasdiff": verify_oasdiff(oasdiff, oasdiff_version, output),
        "yq": verify_yq(yq, yq_version, output),
    }

    # Explicit, locked-down policy prevents a provider-local .oasdiff file or
    # environment config from silently suppressing findings. No uploads.
    policy_file = output / "oasdiff-policy.json"
    policy_file.write_text("{}\n", encoding="utf-8")
    levels_file = output / "oasdiff-levels.txt"
    levels_file.write_text("".join(f"{rule} {level}\n" for rule, level in severity_overrides.items()), encoding="utf-8")
    report["policy"] = {
        "config_file": policy_file.name, "config_sha256": digest(policy_file),
        "severity_file": levels_file.name, "severity_sha256": digest(levels_file),
        "severity_overrides": severity_overrides, "fail_on": "WARN", "match_path": match_path,
    }

    baselines, baseline_artifact, explicit = contract_baselines(args, descriptor, contract)
    for ref, _ in baselines:
        validate_baseline_ref(ref)
    baseline_path = baseline_artifact or declared_artifact
    for ref, source in baselines:
        sha = resolve_commit(repo, ref)
        if not exists_at(repo, sha, baseline_path):
            if explicit:
                raise CheckError(
                    f"Contract {contract_id!r} names baseline {sha} explicitly, but {baseline_path} "
                    "does not exist there; fix the baseline, or set baseline.artifact if the file was moved"
                )
            moved, catch_all = find_moved_artifact(repo, sha, match_path, yq, output, baseline_path)
            if moved and catch_all:
                raise CheckError(
                    f"{declared_artifact} does not exist at baseline {sha}, but {moved} does, and contract "
                    f"{contract_id!r} has a catch-all matchPath, so it cannot tell whether the API is new or "
                    f"was renamed. Set a specific matchPath, or declare baseline: {{refs: [{sha}], "
                    f"artifact: {moved}, reason: ...}} if it was renamed"
                )
            if moved:
                raise CheckError(
                    f"{declared_artifact} does not exist at baseline {sha}, but {moved} there already serves "
                    f"paths matching {match_path!r}: the artifact was moved or renamed. Declare "
                    f"baseline: {{refs: [{sha}], artifact: {moved}, reason: ...}} on contract {contract_id!r}"
                )
            report["baselines"].append({
                "ref": ref, "sha": sha, "source": source, "status": "new",
                "reason": f"{baseline_path} does not exist at this baseline; the API is new relative to it, so there is nothing to compare",
            })
            continue
        baseline = output / f"baseline-{sha}.yaml"
        raw = command(["git", "--no-replace-objects", "show", f"{sha}:{baseline_path}"], cwd=repo).stdout
        baseline.write_text(raw, encoding="utf-8")
        if not spec_has_paths(yq, baseline, output, match_path):
            raise CheckError(f"Baseline {ref!r} has no paths matching {match_path!r}; wrong API/version")
        validation = validate_spec(oasdiff, policy_file, baseline, output)
        report["baselines"].append({
            "ref": ref, "sha": sha, "source": source, "status": "compared", "file": baseline.name,
            "sha256": digest(baseline), "validation_findings": validation,
        })

    candidate = output / "candidate.yaml"
    generator = contract.get("generator") or descriptor.get("spec", {}).get("candidate", {}).get("generator")
    if not generator or "adapter" not in generator:
        raise CheckError(f"Contract {contract_id!r} has no generator.adapter declared")
    try:
        adapters.generate(generator["adapter"], repo, generator, yq, candidate)
    except adapters.AdapterError as error:
        raise CheckError(str(error)) from error
    report["candidate"] = {
        "head": resolve_commit(repo, "HEAD"), "file": candidate.name, "sha256": digest(candidate),
    }
    if candidate.read_bytes() != (repo / declared_artifact).read_bytes():
        raise CheckError(f"Stale committed {declared_artifact}; regenerate it before committing")
    report["candidate"]["validation_findings"] = validate_spec(oasdiff, policy_file, candidate, output)

    breaking = False
    compared = [baseline for baseline in report["baselines"] if baseline["status"] == "compared"]
    for baseline in compared:
        cmd = [oasdiff, "breaking", "--config", policy_file, "--allow-external-refs=false",
               "--severity-levels", levels_file]
        if flatten_allof:
            cmd.append("--flatten-allof")
        if match_path and match_path != ".*":
            cmd.extend(["--match-path", match_path])
        cmd.extend(["--fail-on", "WARN", "--format", "json", output / baseline["file"], candidate])
        result = command(cmd, cwd=output, check=False)
        baseline["findings"] = findings(result, "breaking")
        baseline["compatible"] = result.returncode == 0
        breaking = breaking or not baseline["compatible"]
    if not compared:
        # Every baseline predates this API. Not a pass on compatibility, and not a failure.
        report["status"] = "new"
    else:
        report["status"] = "breaking" if breaking else "compatible"
    return 1 if breaking and args.enforce else 0


def run_check(args):
    output = args.output_dir.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"Cannot create new output directory: {error}", file=sys.stderr)
        return 2
    report = {
        "status": "error", "mode": "enforce" if args.enforce else "report-only",
        "contract_set": str(args.contract_set), "baselines": [], "errors": [],
    }
    try:
        code = check_compatibility(args, output, report)
    except (CheckError, OSError, ValueError) as error:
        report["errors"].append(str(error))
        print(str(error), file=sys.stderr)
        code = 2
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    label = report.get("contract_id", str(args.contract_set))
    print(f"{label}: {report['status']} ({report['mode']}); report: {output / 'report.json'}")
    return code
