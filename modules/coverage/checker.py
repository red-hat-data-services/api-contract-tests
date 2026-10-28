"""Coverage check: is every API contract in a repository registered?

The compatibility checks only ever compare what a provider's ContractSet
descriptor names, so an OpenAPI document that is added (or a CRD that is
renamed) without touching the descriptor silently drops out of the gate. This
module closes that gap. It discovers every OpenAPI document (and, when given
the CRD glob, every CustomResourceDefinition) tracked in a repository and
reports which ones the configuration accounts for.

Two entry points share the same discovery code:

* ``coverage`` compares the discovered documents with ``.rhoai/contracts.yaml``
  and fails when a document is neither a registered contract, an input of a
  registered generator, nor an explicit, reasoned exclusion.
* ``discover`` needs no descriptor. It inventories the documents and suggests a
  contract shape for each, as a starting point for onboarding a new provider.

Like the other modules it uses the repository's pinned ``yq`` to read the
descriptor, so it adds no dependency beyond what the OpenAPI check already has.
"""

import argparse
import fnmatch
import importlib.util
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_OPENAPI_CHECKER = _HERE.parent / "openapi-compat" / "checker.py"

# A parsed YAML or JSON mapping (the descriptor, a contract entry), or a report row.
Document = dict[str, Any]


def _load_openapi_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("openapi_compat_checker", _OPENAPI_CHECKER)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {_OPENAPI_CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


openapi = _load_openapi_checker()
CheckError = openapi.CheckError

SPEC_EXTENSIONS = {".yaml", ".yml", ".json"}
# Larger files are never hand-written API documents; skipping them keeps a
# stray data dump from slowing every run.
MAX_BYTES = 8 * 1024 * 1024
YAML_OPENAPI = re.compile(r"^(openapi|swagger)\s*:\s*[\"']?\d", re.MULTILINE)
YAML_CRD = re.compile(r"^kind\s*:\s*CustomResourceDefinition\s*$", re.MULTILINE)
# A kustomize patch such as a conversion-webhook or CA-injection patch also says
# `kind: CustomResourceDefinition`, but it has no spec.group or spec.names and is
# not a CRD the compatibility check can compare.
YAML_CRD_GROUP = re.compile(r"^\s+group\s*:\s*\S", re.MULTILINE)
YAML_CRD_NAMES = re.compile(r"^\s+names\s*:", re.MULTILINE)


def repo_files(repo: Path) -> list[str]:
    """Repo-relative paths of tracked files plus untracked, non-ignored ones.

    Including untracked files lets a developer run the check before
    committing a new spec; in CI every file is tracked anyway.
    """
    result = openapi.command(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=repo,
    )
    names = sorted({name for name in result.stdout.split("\0") if name})
    return [name for name in names if (repo / name).is_file()]


def _read_text(repo: Path, relative: str) -> str | None:
    path = repo / relative
    if path.suffix.lower() not in SPEC_EXTENSIONS or path.stat().st_size > MAX_BYTES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_openapi_document(relative: str, text: str | None) -> bool:
    """True when the file's top level declares an ``openapi`` or ``swagger`` version."""
    if text is None:
        return False
    if Path(relative).suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
        except ValueError:
            return False
        return isinstance(parsed, dict) and any(
            isinstance(parsed.get(key), str) for key in ("openapi", "swagger")
        )
    return YAML_OPENAPI.search(text) is not None


def is_crd_manifest(relative: str, text: str | None) -> bool:
    return (
        text is not None and Path(relative).suffix.lower() in {".yaml", ".yml"}
        and YAML_CRD.search(text) is not None
        and YAML_CRD_GROUP.search(text) is not None and YAML_CRD_NAMES.search(text) is not None
    )


def discover_documents(repo: Path) -> tuple[list[str], list[str]]:
    """Return (openapi_documents, crd_manifests) as sorted repo-relative paths."""
    documents: list[str] = []
    crds: list[str] = []
    for relative in repo_files(repo):
        text = _read_text(repo, relative)
        if is_openapi_document(relative, text):
            documents.append(relative)
        elif is_crd_manifest(relative, text):
            crds.append(relative)
    return documents, crds


def normalize(relative: str, what: str) -> str:
    cleaned = PurePosixPath(os.path.normpath(str(relative)))
    if cleaned.is_absolute() or ".." in cleaned.parts:
        raise CheckError(f"{what} {relative!r} must be a repo-relative path without '..'")
    return cleaned.as_posix()


def load_exclusions(descriptor: Document) -> list[dict[str, str]]:
    exclusions = descriptor.get("spec", {}).get("exclusions", [])
    if not isinstance(exclusions, list):
        raise CheckError("spec.exclusions must be a list of {path, reason} entries")
    parsed = []
    for index, entry in enumerate(exclusions, start=1):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            raise CheckError(f"spec.exclusions[{index}] needs a non-empty 'path'")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CheckError(
                f"spec.exclusions[{index}] ({entry['path']}) needs a 'reason'; "
                "an exclusion without a recorded justification is not accepted"
            )
        parsed.append({"path": entry["path"], "reason": reason.strip()})
    return parsed


def generator_for(contract: Document, descriptor: Document) -> Document:
    return contract.get("generator") or descriptor.get("spec", {}).get("candidate", {}).get("generator") or {}


def generator_inputs(repo: Path, generator: Document) -> tuple[set[str], list[str]]:
    """Repo-relative inputs of a generator and a list of dangling references."""
    inputs: set[str] = set()
    dangling: list[str] = []
    adapter = generator.get("adapter")
    if adapter == "openapi.static":
        source = generator.get("source")
        if source:
            relative = normalize(source, "generator.source")
            if (repo / relative).is_file():
                inputs.add(relative)
            else:
                dangling.append(f"generator source {relative} does not exist")
    elif adapter == "openapi.bundle.yq-merge":
        source_dir = generator.get("sourceDir")
        if source_dir:
            source_dir = normalize(source_dir, "generator.sourceDir")
            for pattern in generator.get("sourceGlobs", []):
                matches = sorted((repo / source_dir).glob(pattern))
                if not matches:
                    dangling.append(f"generator sourceGlob {pattern!r} matches no files under {source_dir}")
                for match in matches:
                    if match.is_file():
                        inputs.add(match.relative_to(repo).as_posix())
    return inputs, dangling


def classify_openapi(
    repo: Path, descriptor: Document, documents: Sequence[str],
) -> tuple[list[Document], list[str], list[str], list[tuple[str, str, str]]]:
    """Account for every discovered document against the descriptor."""
    exclusions = load_exclusions(descriptor)
    contracts = descriptor.get("spec", {}).get("contracts", [])
    if not isinstance(contracts, list) or not contracts:
        raise CheckError("Descriptor declares no contracts")

    owners: dict[str, str] = {}
    sources: dict[str, str] = {}
    problems: list[str] = []
    warnings: list[str] = []
    scope: list[tuple[str, str, str]] = []
    for contract in contracts:
        contract_id = contract.get("id", "<unnamed>") if isinstance(contract, dict) else "<invalid>"
        if not isinstance(contract, dict):
            raise CheckError("Every contract must be a mapping")
        if contract.get("type") != "openapi":
            warnings.append(f"contract {contract_id} has type {contract.get('type')!r}; coverage only tracks openapi contracts")
            continue
        artifact = contract.get("declaredArtifact")
        if not artifact:
            problems.append(f"contract {contract_id} declares no declaredArtifact")
            continue
        artifact = normalize(artifact, f"contract {contract_id} declaredArtifact")
        if artifact in owners:
            problems.append(f"{artifact} is declared by both {owners[artifact]} and {contract_id}")
        owners[artifact] = contract_id
        if artifact not in documents:
            problems.append(f"contract {contract_id}: declaredArtifact {artifact} is not an OpenAPI document in this repository")
        else:
            scope.append((contract_id, artifact, contract.get("matchPath", ".*")))
        inputs, dangling = generator_inputs(repo, generator_for(contract, descriptor))
        problems.extend(f"contract {contract_id}: {text}" for text in dangling)
        for path in inputs:
            sources.setdefault(path, contract_id)

    rows: list[Document] = []
    used: set[str] = set()
    for path in documents:
        matched = next((item for item in exclusions if fnmatch.fnmatch(path, item["path"])), None)
        if path in owners:
            rows.append({"path": path, "status": "contract", "contract": owners[path]})
            if matched:
                warnings.append(f"{path} is a registered contract but also matches exclusion {matched['path']!r}")
                used.add(matched["path"])
        elif path in sources:
            rows.append({"path": path, "status": "source", "contract": sources[path]})
            if matched:
                warnings.append(f"{path} is a generator input but also matches exclusion {matched['path']!r}")
                used.add(matched["path"])
        elif matched:
            rows.append({"path": path, "status": "excluded", "reason": matched["reason"]})
            used.add(matched["path"])
        else:
            rows.append({"path": path, "status": "uncovered"})
    for item in exclusions:
        if item["path"] not in used:
            warnings.append(f"exclusion {item['path']!r} matches no OpenAPI document; remove it if it is stale")
    for row in rows:
        if row["status"] == "uncovered":
            problems.append(f"{row['path']} is an OpenAPI document with no contract, generator input, or exclusion")
    return rows, problems, warnings, scope


def baseline_overview(
    repo: Path, descriptor: Document, default_refs: Sequence[str],
) -> tuple[list[Document], list[str]]:
    """List per-contract baselines and flag any newer than the repository default.

    A per-contract baseline that is newer than the default, or keeps fewer of the
    default baselines, loosens protection for that contract, so it is surfaced for
    reviewers. A baseline that cannot be ordered against the default is flagged too.
    Older or equal baselines are listed without a warning.
    """
    spec = descriptor.get("spec", {})
    default = list(default_refs) or ((spec.get("baseline") or {}).get("refs") or [])
    rows: list[Document] = []
    warnings: list[str] = []
    for contract in spec.get("contracts", []):
        block = contract.get("baseline") if isinstance(contract, dict) else None
        if block is None:
            continue
        contract_id = contract.get("id", "<unnamed>")
        refs = block.get("refs") if isinstance(block, dict) else None
        reason = block.get("reason") if isinstance(block, dict) else None
        if not isinstance(refs, list) or not refs:
            raise CheckError(f"contract {contract_id}: baseline.refs must be a non-empty list of full Git SHAs")
        for ref in refs:
            openapi.validate_baseline_ref(ref)
        if not isinstance(reason, str) or not reason.strip():
            raise CheckError(f"contract {contract_id}: baseline needs a 'reason'")
        relation = None
        if default:
            relation = _relation(repo, refs, default)
            if relation == "newer":
                warnings.append(
                    f"contract {contract_id}: baseline {refs[0][:12]} is newer than the default baseline; "
                    "this loosens protection for this contract"
                )
            elif relation == "narrower":
                dropped = ", ".join(ref[:12] for ref in default if ref not in refs)
                warnings.append(
                    f"contract {contract_id}: baseline is narrower than the default (drops {dropped}); "
                    "this loosens protection for this contract"
                )
            elif relation == "diverged":
                warnings.append(
                    f"contract {contract_id}: baseline is neither older nor newer than the default; "
                    "check that it does not loosen protection"
                )
            elif relation == "unresolved":
                warnings.append(f"contract {contract_id}: could not compare its baseline with the default (fetch the commits first)")
        rows.append({
            "contract": contract_id, "refs": refs, "reason": reason.strip(),
            "artifact": block.get("artifact"), "relation_to_default": relation,
        })
    return rows, warnings


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    result = openapi.command(["git", "--no-replace-objects", "merge-base", "--is-ancestor", older, newer], cwd=repo, check=False)
    if result.returncode not in (0, 1):
        raise CheckError("unresolved")
    return result.returncode == 0


def _relation(repo: Path, refs: Sequence[str], default: Sequence[str]) -> str:
    try:
        if set(refs) == set(default):
            return "same"
        if set(refs) < set(default):
            # Fewer baselines than the default: the dropped ones are no longer kept compatible.
            return "narrower"
        if all(_is_ancestor(repo, d, r) for r in refs for d in default):
            return "newer"
        if all(_is_ancestor(repo, r, d) for r in refs for d in default):
            return "older"
        return "diverged"
    except CheckError:
        return "unresolved"


def classify_crds(
    crds: Sequence[str], globs: Sequence[str], excludes: Sequence[str] = (),
) -> tuple[list[Document], list[str], list[str]]:
    """Match CRD manifests against the globs the CRD check uses.

    A manifest the check's globs match is covered. One it does not match is
    uncovered unless an explicit --crd-exclude glob names it (for example mocked
    CRDs used as test data).
    """
    rows: list[Document] = []
    problems: list[str] = []
    warnings: list[str] = []
    matched_globs: set[str] = set()
    used_excludes: set[str] = set()
    for path in crds:
        # Record every pattern that matches, not just the first: overlapping globs are
        # legitimate, and a pattern shadowed by an earlier one is not "matching nothing".
        hits = [pattern for pattern in globs if fnmatch.fnmatch(path, pattern)]
        skips = [pattern for pattern in excludes if fnmatch.fnmatch(path, pattern)]
        if hits:
            matched_globs.update(hits)
            rows.append({"path": path, "status": "covered", "glob": hits[0], "globs": hits})
        elif skips:
            used_excludes.update(skips)
            rows.append({"path": path, "status": "excluded", "reason": f"--crd-exclude {skips[0]}", "excludes": skips})
        else:
            rows.append({"path": path, "status": "uncovered"})
            problems.append(f"{path} is a CustomResourceDefinition matched by none of the --crd-glob patterns")
    for pattern in globs:
        if pattern not in matched_globs:
            problems.append(f"--crd-glob {pattern!r} matches no CustomResourceDefinition manifest")
    for pattern in excludes:
        if pattern not in used_excludes:
            warnings.append(f"--crd-exclude {pattern!r} matches no uncovered CRD manifest; remove it if it is stale")
    return rows, problems, warnings


def load_document(yq: Path, repo: Path, relative: str) -> Any:
    path = repo / relative
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(openapi.command([yq, "eval", "-o=json", ".", str(path)], cwd=repo).stdout)


def path_scope(yq: Path, repo: Path, contract_id: str, artifact: str, match_path: str) -> Document:
    """How many of the artifact's API paths a contract's matchPath leaves unchecked."""
    parsed = load_document(yq, repo, artifact)
    names = sorted(parsed.get("paths") or {}) if isinstance(parsed, dict) else []
    try:
        pattern = re.compile(match_path)
    except re.error as error:
        raise CheckError(f"contract {contract_id}: matchPath {match_path!r} is not a valid regular expression: {error}") from error
    outside = [name for name in names if not pattern.match(name)]
    return {
        "contract": contract_id, "artifact": artifact, "match_path": match_path,
        "paths": len(names), "outside_match_path": len(outside), "examples": outside[:5],
    }


VERSION_SEGMENT = re.compile(r"v\d+((alpha|beta)\d+)?")


def common_prefix(names: Sequence[str]) -> str:
    """Longest shared path prefix, cut after the API version segment when there is one."""
    split = [name.strip("/").split("/") for name in names]
    if not split:
        return ""
    prefix: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        prefix.append(parts[0])
        if VERSION_SEGMENT.fullmatch(parts[0]):
            break
    return "/" + "/".join(prefix) if prefix else ""


def describe(yq: Path, repo: Path, relative: str, documents: set[str]) -> Document:
    """Suggest a contract shape for one discovered document (a hint, not a decision)."""
    parsed = load_document(yq, repo, relative)
    info = parsed.get("info") or {} if isinstance(parsed, dict) else {}
    names = sorted((parsed.get("paths") or {}) if isinstance(parsed, dict) else {})
    prefix = common_prefix(names)
    posix = PurePosixPath(relative)
    sibling = posix.parent / "src" / posix.name
    if sibling.as_posix() in documents:
        kind = "bundled"
    elif "src" in posix.parent.parts:
        kind = "source"
    else:
        kind = "standalone"
    record: Document = {
        "path": relative, "kind": kind, "title": info.get("title"), "version": info.get("version"),
        "paths": len(names), "path_prefix": prefix,
        "suggested_match_path": f"^{re.escape(prefix)}(/|$)" if prefix else ".*",
    }
    if kind == "bundled":
        record["suggested_generator"] = {
            "adapter": "openapi.bundle.yq-merge", "sourceDir": sibling.parent.as_posix(),
            "entrypointArg": posix.name, "output": relative,
        }
    elif kind == "standalone":
        record["suggested_generator"] = {"adapter": "openapi.static", "source": relative}
    return record


def bundler_script_candidates(repo: Path, files: Sequence[str]) -> list[str]:
    """Shell scripts that mention both openapi and yq, likely bundlers."""
    found = []
    for relative in files:
        if not relative.endswith(".sh"):
            continue
        text = _read_text_any(repo, relative)
        if text and "openapi" in text.lower() and re.search(r"\byq\b|\$\{?YQ\b", text):
            found.append(relative)
    return found


def _read_text_any(repo: Path, relative: str) -> str | None:
    try:
        path = repo / relative
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def render_coverage(report: Document) -> str:
    lines = [f"Coverage of {report['repo']} against {report['contract_set']}"]
    labels = {"contract": "contract", "source": "source", "excluded": "excluded",
              "uncovered": "UNCOVERED", "covered": "covered"}
    if report["openapi"]:
        lines.append("")
        lines.append("OpenAPI documents:")
        for row in report["openapi"]:
            detail = row.get("contract") or row.get("reason") or ""
            if row["status"] == "source":
                detail = f"generator input of {detail}"
            elif row["status"] == "contract":
                detail = f"contract {detail}"
            lines.append(f"  {labels[row['status']]:<10} {row['path']}  {detail}".rstrip())
    if report["crds"]:
        lines.append("")
        lines.append("CustomResourceDefinition manifests:")
        for row in report["crds"]:
            lines.append(f"  {labels[row['status']]:<10} {row['path']}  {row.get('glob') or row.get('reason', '')}".rstrip())
    elif report["crd_manifests_found"]:
        lines.append("")
        lines.append(f"{report['crd_manifests_found']} CRD manifest(s) found; pass --crd-glob to check their coverage.")
    unchecked = [item for item in report["path_scope"] if item["outside_match_path"]]
    if unchecked:
        lines.append("")
        lines.append("Paths left unchecked by matchPath (intentional scoping, not a failure):")
        for item in unchecked:
            lines.append(
                f"  {item['contract']}: {item['outside_match_path']} of {item['paths']} paths in "
                f"{item['artifact']} fall outside {item['match_path']!r}"
            )
    if report["baselines"]:
        lines.append("")
        lines.append("Per-contract baselines (override the repository default):")
        for item in report["baselines"]:
            shas = ", ".join(ref[:12] for ref in item["refs"])
            kind = item["relation_to_default"]
            relation = f" [{kind} than default]" if kind in ("newer", "older", "narrower") else (" [diverged from default]" if kind == "diverged" else "")
            lines.append(f"  {item['contract']}: {shas}{relation}  {item['reason']}")
    for text in report["warnings"]:
        lines.append(f"warning: {text}")
    for text in report["problems"]:
        lines.append(f"problem: {text}")
    lines.append("")
    lines.append(f"Result: {report['status']}" + (f" ({len(report['problems'])} problem(s))" if report["problems"] else ""))
    return "\n".join(lines)


def run_coverage(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    report: Document = {
        "status": "error", "repo": str(repo), "contract_set": str(args.contract_set),
        "openapi": [], "crds": [], "crd_manifests_found": 0, "path_scope": [], "baselines": [],
        "problems": [], "warnings": [], "errors": [],
    }
    code = 2
    try:
        documents, crds = discover_documents(repo)
        if (repo / args.contract_set).is_file():
            yq = openapi.tool_path("yq", args.yq, repo)
            descriptor = openapi.load_descriptor(repo / args.contract_set, yq, repo)
            versions = {
                contract.get("policy", {}).get("yqVersion")
                for contract in descriptor.get("spec", {}).get("contracts", []) if isinstance(contract, dict)
            } - {None}
            if len(versions) > 1:
                raise CheckError(f"Descriptor pins conflicting yq versions: {sorted(versions)}")
            for version in versions:
                openapi.verify_yq(yq, version, repo)
            rows, problems, warnings, scope = classify_openapi(repo, descriptor, documents)
            report["openapi"], report["problems"], report["warnings"] = rows, problems, warnings
            report["path_scope"] = [path_scope(yq, repo, *item) for item in scope]
            report["baselines"], baseline_warnings = baseline_overview(repo, descriptor, args.baseline_ref)
            report["warnings"].extend(baseline_warnings)
        elif args.crd_glob and not documents:
            # A repository that ships only CRDs has no OpenAPI contracts to register,
            # so there is no descriptor to read; only the CRD globs are checked.
            report["warnings"].append(f"{args.contract_set} not found; no OpenAPI documents, so only CRD coverage was checked")
        else:
            raise CheckError(f"Descriptor not found: {repo / args.contract_set}")
        report["crd_manifests_found"] = len(crds)
        if args.crd_glob:
            crd_rows, crd_problems, crd_warnings = classify_crds(crds, args.crd_glob, args.crd_exclude)
            report["crds"] = crd_rows
            report["problems"].extend(crd_problems)
            report["warnings"].extend(crd_warnings)
        report["status"] = "gaps" if report["problems"] else "complete"
        code = 1 if report["problems"] and not args.report_only else 0
    except (CheckError, OSError, ValueError) as error:
        report["errors"].append(str(error))
        print(str(error), file=sys.stderr)

    if getattr(args, "json_output", None):
        args.json_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        print(json.dumps(report, indent=2))
    elif report["errors"]:
        print(f"Result: error ({report['errors'][0]})")
    else:
        print(render_coverage(report))
    return code


def run_discover(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    try:
        yq = openapi.tool_path("yq", args.yq, repo)
        files = repo_files(repo)
        documents, crds = discover_documents(repo)
        described = [describe(yq, repo, path, set(documents)) for path in documents]
        report = {
            "repo": str(repo), "openapi": described, "crd_manifests": crds,
            "bundler_script_candidates": bundler_script_candidates(repo, files),
            "descriptor_present": (repo / ".rhoai" / "contracts.yaml").is_file(),
        }
    except (CheckError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, indent=2))
        return 0
    lines = [f"Discovered in {report['repo']}:", ""]
    lines.append(f"OpenAPI documents ({len(described)}):")
    for item in described:
        lines.append(f"  {item['kind']:<10} {item['path']}  ({item['paths']} paths, prefix {item['path_prefix'] or '-'})")
    lines.append("")
    lines.append(f"CRD manifests ({len(crds)}):")
    lines.extend(f"  {path}" for path in crds)
    if report["bundler_script_candidates"]:
        lines.append("")
        lines.append("Possible bundler scripts (mention openapi and yq):")
        lines.extend(f"  {path}" for path in report["bundler_script_candidates"])
    lines.append("")
    lines.append("Descriptor " + ("present at .rhoai/contracts.yaml; run `coverage` next." if report["descriptor_present"]
                                  else "not found; draft .rhoai/contracts.yaml from this inventory (see docs/onboarding)."))
    print("\n".join(lines))
    return 0
