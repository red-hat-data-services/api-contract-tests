"""Generic CRD compatibility engine.

Compares a provider pull request's CustomResourceDefinitions against immutable
Git baselines. The initial policy catches removals, newly required fields,
type changes, enum narrowing, default changes, served-version removal, and
storage-version changes without requiring a Kubernetes cluster.
"""

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


class CheckError(Exception):
    """The comparison could not be completed reliably."""


def command(args, *, cwd, check=True):
    try:
        result = subprocess.run(
            [str(arg) for arg in args], cwd=cwd,
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"Cannot run {args[0]}: {error}") from error
    if check and result.returncode:
        raise CheckError(f"{args[0]} failed: {result.stderr or result.stdout}")
    return result


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
        raise CheckError(f"Cannot resolve revision {ref!r}; fetch the trusted revision first")
    return sha


def digest(data):
    return hashlib.sha256(data).hexdigest()


def candidate_files(repo, pattern):
    files = sorted(
        path for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
        and fnmatch.fnmatch(path.relative_to(repo).as_posix(), pattern)
    )
    if not files:
        raise CheckError(f"No candidate files match CRD glob {pattern!r}")
    return files


def baseline_files(repo, ref, pattern):
    result = command(["git", "ls-tree", "-r", "--name-only", ref], cwd=repo)
    return sorted(
        Path(line) for line in result.stdout.splitlines()
        if line and fnmatch.fnmatch(line, pattern)
    )


def baseline_has_crds(repo, ref):
    """True when the baseline contains any CRD manifest, wherever it lives.

    Separates "this provider had no CRDs yet" (new, nothing to compare) from "the
    glob no longer matches the CRDs the baseline has" (a configuration error).
    """
    result = command(
        ["git", "--no-replace-objects", "grep", "-l", "-I", "-E", r"^kind[[:space:]]*:[[:space:]]*CustomResourceDefinition",
         ref, "--", "*.yaml", "*.yml"],
        cwd=repo, check=False,
    )
    prefix = f"{ref}:"
    for line in result.stdout.splitlines():
        relative = line[len(prefix):] if line.startswith(prefix) else line
        content = command(["git", "show", f"{ref}:{relative}"], cwd=repo, check=False).stdout
        if re.search(r"^\s+group\s*:\s*\S", content, re.MULTILINE) and re.search(r"^\s+names\s*:", content, re.MULTILINE):
            return True
    return False


def git_file(repo, ref, relative_path):
    result = command(["git", "show", f"{ref}:{relative_path.as_posix()}"], cwd=repo)
    return result.stdout.encode("utf-8")


def parse_crds(content, source):
    try:
        documents = list(yaml.safe_load_all(content))
    except yaml.YAMLError as error:
        raise CheckError(f"Cannot parse CRD YAML {source}: {error}") from error

    crds = {}
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict) or document.get("kind") != "CustomResourceDefinition":
            raise CheckError(f"{source} document {index} is not a CustomResourceDefinition")
        metadata = document.get("metadata") or {}
        spec = document.get("spec") or {}
        names = spec.get("names") or {}
        group = spec.get("group")
        kind = names.get("kind")
        if not isinstance(group, str) or not group or not isinstance(kind, str) or not kind:
            raise CheckError(f"{source} document {index} is missing spec.group or spec.names.kind")
        key = (group, kind)
        if key in crds:
            raise CheckError(f"Duplicate CRD {group}/{kind} in {source}")
        crds[key] = document
    if not crds:
        raise CheckError(f"{source} contains no CustomResourceDefinition documents")
    return crds


def load_candidate(repo, files):
    documents = {}
    records = []
    for path in files:
        content = path.read_bytes()
        relative = path.relative_to(repo).as_posix()
        documents.update(parse_crds(content, relative))
        records.append({"file": relative, "sha256": digest(content)})
    return documents, records


def load_baseline(repo, ref, files):
    documents = {}
    records = []
    for path in files:
        content = git_file(repo, ref, path)
        relative = path.as_posix()
        documents.update(parse_crds(content, f"{ref}:{relative}"))
        records.append({"file": relative, "sha256": digest(content)})
    return documents, records


def finding(identifier, path, text):
    return {"id": identifier, "path": path, "text": text, "severity": "error"}


def compare_schema(path, baseline, candidate, findings):
    if baseline is None:
        return
    if candidate is None:
        findings.append(finding("property-removed", path, "removed a schema field used by the baseline"))
        return

    if isinstance(baseline, dict) and isinstance(candidate, dict):
        baseline_type = baseline.get("type")
        candidate_type = candidate.get("type")
        if baseline_type is not None and candidate_type is not None and baseline_type != candidate_type:
            findings.append(finding(
                "type-changed", path,
                f"changed type from {baseline_type!r} to {candidate_type!r}",
            ))

        baseline_required = set(baseline.get("required") or [])
        candidate_required = set(candidate.get("required") or [])
        for name in sorted(candidate_required - baseline_required):
            findings.append(finding(
                "required-property-added", f"{path}.required.{name}",
                f"added required property {name!r}",
            ))

        baseline_enum = baseline.get("enum")
        candidate_enum = candidate.get("enum")
        if isinstance(baseline_enum, list) and isinstance(candidate_enum, list):
            removed = [value for value in baseline_enum if value not in candidate_enum]
            if removed:
                findings.append(finding(
                    "enum-value-removed", f"{path}.enum",
                    f"removed enum values {removed!r}",
                ))

        if "default" in baseline and "default" in candidate and baseline["default"] != candidate["default"]:
            findings.append(finding(
                "default-changed", f"{path}.default",
                f"changed default from {baseline['default']!r} to {candidate['default']!r}",
            ))

        properties = baseline.get("properties") or {}
        candidate_properties = candidate.get("properties") or {}
        for name, schema in properties.items():
            compare_schema(
                f"{path}.properties.{name}", schema,
                candidate_properties.get(name), findings,
            )

        if isinstance(baseline.get("items"), dict):
            compare_schema(
                f"{path}.items", baseline["items"], candidate.get("items"), findings,
            )
        if isinstance(baseline.get("additionalProperties"), dict):
            compare_schema(
                f"{path}.additionalProperties", baseline["additionalProperties"],
                candidate.get("additionalProperties"), findings,
            )


def compare_crd(key, baseline, candidate, findings):
    group, kind = key
    label = f"{group}/{kind}"
    baseline_versions = {
        version.get("name"): version
        for version in baseline.get("spec", {}).get("versions", [])
        if isinstance(version, dict) and isinstance(version.get("name"), str)
    }
    candidate_versions = {
        version.get("name"): version
        for version in candidate.get("spec", {}).get("versions", [])
        if isinstance(version, dict) and isinstance(version.get("name"), str)
    }

    for name, baseline_version in baseline_versions.items():
        path = f"{label}/versions/{name}"
        candidate_version = candidate_versions.get(name)
        if candidate_version is None:
            findings.append(finding("crd-version-removed", path, "removed a CRD API version"))
            continue
        if baseline_version.get("served") is True and candidate_version.get("served") is not True:
            findings.append(finding("crd-version-no-longer-served", path, "stopped serving a baseline CRD API version"))
        if baseline_version.get("storage") is True and candidate_version.get("storage") is not True:
            findings.append(finding("crd-storage-version-changed", path, "removed storage designation from a baseline CRD API version"))

        compare_schema(
            f"{path}.openAPIV3Schema",
            baseline_version.get("schema", {}).get("openAPIV3Schema"),
            candidate_version.get("schema", {}).get("openAPIV3Schema"),
            findings,
        )


def compare(baseline, candidate):
    findings = []
    for key, baseline_crd in baseline.items():
        candidate_crd = candidate.get(key)
        if candidate_crd is None:
            findings.append(finding(
                "crd-removed", f"{key[0]}/{key[1]}",
                "removed a CustomResourceDefinition present in the baseline",
            ))
            continue
        compare_crd(key, baseline_crd, candidate_crd, findings)
    return findings


def run_check(args):
    output = args.output_dir.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"Cannot create new output directory: {error}", file=sys.stderr)
        return 2

    report = {
        "status": "error",
        "mode": "enforce" if args.enforce else "report-only",
        "crd_glob": args.crd_glob,
        "baselines": [],
        "errors": [],
    }
    try:
        candidates = candidate_files(args.repo, args.crd_glob)
        candidate_documents, candidate_records = load_candidate(args.repo, candidates)
        report["candidate"] = {
            "head": resolve_commit(args.repo, "HEAD"),
            "files": candidate_records,
        }

        breaking = False
        for ref in args.baseline_ref:
            validate_baseline_ref(ref)
            files = baseline_files(args.repo, ref, args.crd_glob)
            if not files:
                if baseline_has_crds(args.repo, ref):
                    raise CheckError(f"No baseline files match CRD glob {args.crd_glob!r} at {ref}, but the baseline has CRD manifests; the glob is probably wrong")
                report["baselines"].append({
                    "ref": ref, "sha": resolve_commit(args.repo, ref), "status": "new",
                    "reason": "the baseline has no CRD manifests; the CRDs are new relative to it, so there is nothing to compare",
                })
                continue
            baseline_documents, baseline_records = load_baseline(args.repo, ref, files)
            findings = compare(baseline_documents, candidate_documents)
            compatible = not findings
            breaking = breaking or not compatible
            report["baselines"].append({
                "ref": ref,
                "sha": resolve_commit(args.repo, ref),
                "status": "compared",
                "files": baseline_records,
                "findings": findings,
                "compatible": compatible,
            })

        if all(baseline["status"] == "new" for baseline in report["baselines"]):
            report["status"] = "new"
        else:
            report["status"] = "breaking" if breaking else "compatible"
        code = 1 if breaking and args.enforce else 0
    except (CheckError, OSError, ValueError) as error:
        report["errors"].append(str(error))
        print(str(error), file=sys.stderr)
        code = 2

    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    label = args.crd_glob
    print(f"{label}: {report['status']} ({report['mode']}); report: {output / 'report.json'}")
    return code


def build_parser():
    parser = argparse.ArgumentParser(prog="contract-tests crd-check")
    check = parser
    check.add_argument("--repo", type=Path, required=True)
    check.add_argument("--crd-glob", required=True)
    check.add_argument("--baseline-ref", action="append", required=True)
    check.add_argument("--output-dir", type=Path, required=True)
    check.add_argument("--enforce", action="store_true")
    return parser
