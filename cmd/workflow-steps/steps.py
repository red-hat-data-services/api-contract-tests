#!/usr/bin/env python3
"""Steps of the provider workflows, kept out of the YAML so they can be tested.

Each provider workflow (.github/workflows/provider-*.yml) checks out this
repository at its own commit and calls one subcommand per step. Inputs arrive as
environment variables, exactly as the workflow passes them:

    steps.py require-pinned-ref       WORKFLOW_REF, WORKFLOW_SHA, WORKFLOW_REPOSITORY, CALLER_REPOSITORY
    steps.py run {openapi,crd,coverage}
                                      MODE, BASELINE_REFS, and per check: CONTRACT_SET,
                                      CONTRACT_ID, CRD_GLOB, CRD_GLOBS, CRD_EXCLUDE_GLOBS
    steps.py summarize {openapi,crd}  report at .contract-report/report.json
    steps.py annotate-coverage        MODE, report at $RUNNER_TEMP/coverage.json

Markdown goes to the job summary ($GITHUB_STEP_SUMMARY; stdout when unset).
Workflow commands (::error:: and friends) go to stdout.

This runs on the runner's system Python, so it uses the standard library only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

CLI = Path(__file__).resolve().parents[1] / "contract-tests" / "cli.py"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
REPORT = Path(".contract-report") / "report.json"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class InputError(Exception):
    """A workflow input is invalid; the step fails before running any check."""


def escape(text: str) -> str:
    # Workflow-command data must not carry raw %, CR or LF.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def command(level: str, message: str, title: str | None = None) -> str:
    properties = f" title={escape(title)}" if title else ""
    return f"::{level}{properties}::{escape(message)}"


def require_pinned_ref(env: Mapping[str, str]) -> tuple[int, str]:
    """Require callers to name this workflow by a full commit SHA.

    job.workflow_sha is always the resolved commit, even when the caller wrote a
    branch or tag, so it cannot show how the caller pinned. job.workflow_ref ends
    with the ref the caller wrote after '@'; require a full commit SHA there.

    The exception is this repository calling its own workflows with
    ``uses: ./.github/workflows/...`` (the self-test). A local call has no SHA to
    pin, and it always runs the workflow from the caller's own commit. A provider is
    a different repository, so it can never match.
    """
    workflow_ref = env.get("WORKFLOW_REF", "")
    workflow_repository = env.get("WORKFLOW_REPOSITORY", "")
    if workflow_repository and workflow_repository == env.get("CALLER_REPOSITORY"):
        return 0, command("notice", f"{workflow_repository} is calling its own workflow; pinning is not required: {workflow_ref}")
    pinned = workflow_ref.rpartition("@")[2]
    if FULL_SHA.fullmatch(pinned) is None or pinned != env.get("WORKFLOW_SHA"):
        return 2, command("error", f"Call this workflow by a full commit SHA, not a branch or tag: {workflow_ref}")
    return 0, ""


def _mode(env: Mapping[str, str]) -> str:
    mode = env.get("MODE", "report")
    if mode not in ("report", "enforce"):
        raise InputError(f"mode must be report or enforce, not {mode!r}")
    return mode


def _baseline_args(env: Mapping[str, str]) -> list[str]:
    args = []
    for ref in env.get("BASELINE_REFS", "").split():
        if FULL_SHA.fullmatch(ref) is None:
            raise InputError(f"Baseline refs must be full 40-character Git SHAs: {ref}")
        args += ["--baseline-ref", ref]
    return args


def check_args(check: str, env: Mapping[str, str], workspace: Path) -> list[str]:
    """The cli.py arguments for one check, built from the workflow's inputs.

    Space-separated inputs are split on whitespace and never glob-expanded; the
    checkers expand CRD globs themselves.
    """
    mode = _mode(env)
    report_dir = str(workspace / ".contract-report")
    if check == "openapi":
        args = ["check", "--repo", str(workspace), "--contract-set", env.get("CONTRACT_SET", ""),
                "--output-dir", report_dir]
        if env.get("CONTRACT_ID"):
            args += ["--contract-id", env["CONTRACT_ID"]]
        args += _baseline_args(env)
        return args + (["--enforce"] if mode == "enforce" else [])
    if check == "crd":
        if not env.get("CRD_GLOB"):
            raise InputError("crd-glob is required")
        args = ["crd-check", "--repo", str(workspace), "--crd-glob", env["CRD_GLOB"], "--output-dir", report_dir]
        args += _baseline_args(env)
        return args + (["--enforce"] if mode == "enforce" else [])
    if check == "coverage":
        args = ["coverage", "--repo", str(workspace), "--contract-set", env.get("CONTRACT_SET", "")]
        for glob in env.get("CRD_GLOBS", "").split():
            args += ["--crd-glob", glob]
        for glob in env.get("CRD_EXCLUDE_GLOBS", "").split():
            args += ["--crd-exclude", glob]
        args += _baseline_args(env)
        if mode == "report":
            args.append("--report-only")
        # The JSON copy stays outside the checkout so it is never scanned as a repository file.
        return args + ["--json-output", str(coverage_report(env))]
    raise InputError(f"unknown check {check!r}")


def coverage_report(env: Mapping[str, str]) -> Path:
    return Path(env.get("RUNNER_TEMP", ".")) / "coverage.json"


def run_check(check: str, env: Mapping[str, str], workspace: Path, runner: Runner = subprocess.run) -> int:
    try:
        args = check_args(check, env, workspace)
    except InputError as error:
        print(command("error", str(error)))
        return 2
    if check != "coverage":
        return runner([sys.executable, str(CLI), *args], check=False).returncode
    # The coverage text goes to the job log and the job summary.
    result = runner([sys.executable, str(CLI), *args], check=False, stdout=subprocess.PIPE, text=True)
    print(result.stdout, end="")
    with summary_stream(env) as summary:
        summary.write(result.stdout)
    return result.returncode


@contextmanager
def summary_stream(env: Mapping[str, str]) -> Iterator[TextIO]:
    """The job summary file, or stdout outside GitHub Actions."""
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        yield sys.stdout
        return
    with open(path, "a", encoding="utf-8") as stream:
        yield stream


def _findings(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for baseline in report.get("baselines", []) for item in baseline.get("findings", [])]


def summarize(check: str, report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Markdown summary lines and workflow-command lines for a compatibility report."""
    mode = report.get("mode", "unknown mode")
    status = report.get("status", "error")
    findings = _findings(report)
    if check == "crd":
        label = report.get("crd_glob", "unknown glob")
        heading, prefix = f"CRD compatibility: {label}", "CRD compatibility"
    else:
        label = report.get("contract_id", report.get("contract_set", "unknown contract"))
        heading, prefix = f"OpenAPI compatibility: {label}", label

    lines = [f"## {heading}", "", f"- **Mode:** {mode}", f"- **Result:** {status}", f"- **Findings:** {len(findings)}"]
    if check == "openapi":
        lines += ["", "| Baseline | Source | Compatible | Findings |", "| --- | --- | --- | --- |"]
        for baseline in report.get("baselines", []):
            if baseline.get("status") == "new":
                compatible = "new (not in baseline)"
            else:
                compatible = "yes" if baseline.get("compatible") else "no"
            lines.append(f"| `{baseline.get('sha', 'unknown')}` | {baseline.get('source', 'default')} | "
                         f"{compatible} | {len(baseline.get('findings', []))} |")
        if status == "new":
            lines += ["", "This API does not exist at any baseline yet, so nothing was compared. "
                          "It becomes protected once a baseline that contains it is adopted."]

    commands = []
    if status == "breaking":
        message = (f"{prefix}: {status} ({mode}); {len(findings)} breaking findings. "
                   "Download the contract-report artifact for details.")
        commands.append(command("warning" if mode == "report-only" else "error", message))
        lines += ["", "### Breaking findings", ""]
        for item in findings:
            text = str(item.get("text", "")).replace("|", "\\|")
            where = f"{item.get('operation', '')} {item.get('path', '')}".strip()
            lines.append(f"- `{where}` — {text}")
    elif status == "error":
        errors = "; ".join(report.get("errors", [])) or "unknown error"
        commands.append(command("error", f"{prefix}: {status} ({mode}); {errors}"))
    else:
        commands.append(command("notice", f"{prefix}: {status} ({mode}); {len(findings)} findings."))
    return lines, commands


def summarize_report(check: str, env: Mapping[str, str], report_path: Path = REPORT) -> int:
    name = "CRD compatibility check" if check == "crd" else "Compatibility check"
    if not report_path.is_file():
        print(command("error", f"{name} did not produce {report_path.name}"))
        return 1
    lines, commands = summarize(check, json.loads(report_path.read_text(encoding="utf-8")))
    with summary_stream(env) as summary:
        summary.write("\n".join(lines) + "\n")
    print("\n".join(commands))
    return 0


def coverage_annotations(report: Mapping[str, Any] | None, mode: str) -> list[str]:
    if report is None:
        return [command("error", "The coverage check did not produce a report", title="API contract coverage")]
    # Report mode keeps the job green, so a warning is the only signal on the pull request.
    level = "error" if mode == "enforce" else "warning"
    lines = [command(level, text, title="API contract coverage gap") for text in report.get("problems", [])]
    lines += [command("error", text, title="API contract coverage error") for text in report.get("errors", [])]
    return lines


def annotate_coverage(env: Mapping[str, str]) -> int:
    path = coverage_report(env)
    report = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    for line in coverage_annotations(report, env.get("MODE", "report")):
        print(line)
    # Never fails the job: the coverage step's exit code already decides that.
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="step", required=True)
    subparsers.add_parser("require-pinned-ref")
    subparsers.add_parser("run").add_argument("check", choices=("openapi", "crd", "coverage"))
    subparsers.add_parser("summarize").add_argument("check", choices=("openapi", "crd"))
    subparsers.add_parser("annotate-coverage")
    args = parser.parse_args(argv)

    env = os.environ
    if args.step == "require-pinned-ref":
        code, message = require_pinned_ref(env)
        if message:
            print(message)
        return code
    if args.step == "run":
        return run_check(args.check, env, Path.cwd())
    if args.step == "summarize":
        return summarize_report(args.check, env)
    return annotate_coverage(env)


if __name__ == "__main__":
    sys.exit(main())
