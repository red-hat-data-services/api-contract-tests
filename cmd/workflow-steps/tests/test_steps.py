"""Tests for the provider workflow steps (cmd/workflow-steps/steps.py)."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "cmd" / "workflow-steps" / "steps.py"

spec = importlib.util.spec_from_file_location("workflow_steps", MODULE)
steps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(steps)


SHA = "a" * 40
OTHER = "b" * 40
WORKSPACE = Path("/work/provider")


# require-pinned-ref

def pinned_env(ref, sha=SHA, repository="red-hat-data-services/api-contract-tests", caller="org/provider"):
    return {
        "WORKFLOW_REF": f"{repository}/.github/workflows/provider-crd-check.yml@{ref}",
        "WORKFLOW_SHA": sha, "WORKFLOW_REPOSITORY": repository, "CALLER_REPOSITORY": caller,
    }


def test_provider_pinned_by_sha_passes():
    assert steps.require_pinned_ref(pinned_env(SHA)) == (0, "")


@pytest.mark.parametrize("ref", ["main", "v1.0.0", "refs/heads/main", SHA[:12]])
def test_provider_pinned_by_branch_or_tag_fails(ref):
    code, message = steps.require_pinned_ref(pinned_env(ref))
    assert code == 2
    assert message.startswith("::error::Call this workflow by a full commit SHA")


def test_sha_that_is_not_the_running_commit_fails():
    assert steps.require_pinned_ref(pinned_env(OTHER))[0] == 2


def test_self_test_call_from_this_repository_is_exempt():
    env = pinned_env("refs/pull/8/merge", caller="red-hat-data-services/api-contract-tests")
    code, message = steps.require_pinned_ref(env)
    assert code == 0
    assert message.startswith("::notice::red-hat-data-services/api-contract-tests is calling its own workflow")


def test_missing_repositories_do_not_count_as_a_self_call():
    env = pinned_env("main", repository="", caller="")
    assert steps.require_pinned_ref(env)[0] == 2


# run: building cli.py arguments from workflow inputs

def test_openapi_args():
    env = {"MODE": "enforce", "CONTRACT_SET": ".rhoai/contracts.yaml", "CONTRACT_ID": "svc-v1",
           "BASELINE_REFS": f"{SHA} {OTHER}"}
    assert steps.check_args("openapi", env, WORKSPACE) == [
        "check", "--repo", "/work/provider", "--contract-set", ".rhoai/contracts.yaml",
        "--output-dir", "/work/provider/.contract-report", "--contract-id", "svc-v1",
        "--baseline-ref", SHA, "--baseline-ref", OTHER, "--enforce",
    ]


def test_openapi_args_without_contract_id_or_baselines_in_report_mode():
    env = {"MODE": "report", "CONTRACT_SET": "c.yaml", "CONTRACT_ID": "", "BASELINE_REFS": ""}
    assert steps.check_args("openapi", env, WORKSPACE) == [
        "check", "--repo", "/work/provider", "--contract-set", "c.yaml", "--output-dir", "/work/provider/.contract-report",
    ]


def test_crd_args():
    env = {"MODE": "report", "CRD_GLOB": "config/crd/**/*.yaml", "BASELINE_REFS": SHA}
    assert steps.check_args("crd", env, WORKSPACE) == [
        "crd-check", "--repo", "/work/provider", "--crd-glob", "config/crd/**/*.yaml",
        "--output-dir", "/work/provider/.contract-report", "--baseline-ref", SHA,
    ]


def test_crd_glob_is_required():
    with pytest.raises(steps.InputError, match="crd-glob is required"):
        steps.check_args("crd", {"MODE": "report", "CRD_GLOB": "", "BASELINE_REFS": SHA}, WORKSPACE)


def test_coverage_args_keep_globs_unexpanded():
    env = {"MODE": "report", "CONTRACT_SET": ".rhoai/contracts.yaml", "CRD_GLOBS": "config/crd/*.yaml  config/overlays/**/*.yaml",
           "CRD_EXCLUDE_GLOBS": "test/*", "BASELINE_REFS": "", "RUNNER_TEMP": "/runner/tmp"}
    assert steps.check_args("coverage", env, WORKSPACE) == [
        "coverage", "--repo", "/work/provider", "--contract-set", ".rhoai/contracts.yaml",
        "--crd-glob", "config/crd/*.yaml", "--crd-glob", "config/overlays/**/*.yaml", "--crd-exclude", "test/*",
        "--report-only", "--json-output", "/runner/tmp/coverage.json",
    ]


def test_coverage_in_enforce_mode_is_not_report_only():
    env = {"MODE": "enforce", "CONTRACT_SET": "c.yaml", "RUNNER_TEMP": "/t"}
    assert "--report-only" not in steps.check_args("coverage", env, WORKSPACE)


@pytest.mark.parametrize("check", ["openapi", "crd", "coverage"])
def test_invalid_mode_is_rejected(check):
    env = {"MODE": "strict", "CONTRACT_SET": "c.yaml", "CRD_GLOB": "x"}
    with pytest.raises(steps.InputError, match="mode must be report or enforce"):
        steps.check_args(check, env, WORKSPACE)


@pytest.mark.parametrize("check", ["openapi", "crd", "coverage"])
def test_mutable_baseline_ref_is_rejected(check):
    env = {"MODE": "report", "CONTRACT_SET": "c.yaml", "CRD_GLOB": "x", "BASELINE_REFS": f"{SHA} main"}
    with pytest.raises(steps.InputError, match="full 40-character Git SHAs: main"):
        steps.check_args(check, env, WORKSPACE)


def test_run_check_reports_invalid_input_as_an_error_without_running(capsys):
    def runner(*args, **kwargs):
        raise AssertionError("the checker must not run")

    assert steps.run_check("crd", {"MODE": "strict", "CRD_GLOB": "x"}, WORKSPACE, runner) == 2
    assert capsys.readouterr().out.startswith("::error::mode must be report or enforce")


def test_run_check_passes_the_checker_exit_code_through():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1)

    assert steps.run_check("crd", {"MODE": "enforce", "CRD_GLOB": "x", "BASELINE_REFS": SHA}, WORKSPACE, runner) == 1
    assert calls[0][1] == str(steps.CLI)
    assert calls[0][2] == "crd-check"


def test_run_coverage_copies_its_output_to_the_job_summary(tmp_path, capsys):
    summary = tmp_path / "summary.md"

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="Result: complete\n")

    env = {"MODE": "report", "CONTRACT_SET": "c.yaml", "RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(summary)}
    assert steps.run_check("coverage", env, WORKSPACE, runner) == 0
    assert capsys.readouterr().out == "Result: complete\n"
    assert summary.read_text() == "Result: complete\n"


def test_cli_path_points_at_the_runner():
    assert steps.CLI == ROOT / "cmd" / "contract-tests" / "cli.py"
    assert steps.CLI.is_file()


# summarize

def openapi_report(status, mode="report-only", **extra):
    report = {"status": status, "mode": mode, "contract_id": "svc-v1", "baselines": [], "errors": []}
    report.update(extra)
    return report


def test_breaking_openapi_report_in_report_mode_is_a_warning_with_findings():
    finding = {"operation": "GET", "path": "/api/v1/items", "text": "removed a|b"}
    report = openapi_report("breaking", baselines=[
        {"sha": SHA, "source": "default", "status": "compared", "compatible": False, "findings": [finding]},
    ])
    lines, commands = steps.summarize("openapi", report)
    assert lines[0] == "## OpenAPI compatibility: svc-v1"
    assert f"| `{SHA}` | default | no | 1 |" in lines
    assert "- `GET /api/v1/items` — removed a\\|b" in lines
    assert commands == ["::warning::svc-v1: breaking (report-only); 1 breaking findings. "
                        "Download the contract-report artifact for details."]


def test_breaking_report_in_enforce_mode_is_an_error():
    report = openapi_report("breaking", mode="enforce", baselines=[{"findings": [{"text": "x"}]}])
    assert steps.summarize("openapi", report)[1][0].startswith("::error::svc-v1: breaking (enforce)")


def test_new_openapi_contract_is_explained():
    report = openapi_report("new", baselines=[{"sha": SHA, "status": "new"}])
    lines, commands = steps.summarize("openapi", report)
    assert f"| `{SHA}` | default | new (not in baseline) | 0 |" in lines
    assert any("does not exist at any baseline yet" in line for line in lines)
    assert commands == ["::notice::svc-v1: new (report-only); 0 findings."]


def test_error_report_annotation_escapes_newlines():
    report = openapi_report("error", errors=["oasdiff failed:\nbad input", "second"])
    assert steps.summarize("openapi", report)[1] == [
        "::error::svc-v1: error (report-only); oasdiff failed:%0Abad input; second",
    ]


def test_crd_summary_uses_the_glob_as_label():
    report = {"status": "compatible", "mode": "report-only", "crd_glob": "config/crd/*.yaml",
              "baselines": [{"findings": []}]}
    lines, commands = steps.summarize("crd", report)
    assert lines[0] == "## CRD compatibility: config/crd/*.yaml"
    assert not any(line.startswith("|") for line in lines)
    assert commands == ["::notice::CRD compatibility: compatible (report-only); 0 findings."]


def test_summarize_report_writes_markdown_to_the_summary_and_commands_to_stdout(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(openapi_report("compatible")))
    summary = tmp_path / "summary.md"
    assert steps.summarize_report("openapi", {"GITHUB_STEP_SUMMARY": str(summary)}, report_path) == 0
    assert summary.read_text().startswith("## OpenAPI compatibility: svc-v1\n")
    assert capsys.readouterr().out == "::notice::svc-v1: compatible (report-only); 0 findings.\n"


def test_missing_report_fails_the_summary_step(tmp_path, capsys):
    assert steps.summarize_report("crd", {}, tmp_path / "report.json") == 1
    assert capsys.readouterr().out == "::error::CRD compatibility check did not produce report.json\n"


# annotate-coverage

def test_coverage_gaps_are_warnings_in_report_mode_and_errors_in_enforce_mode():
    report = {"problems": ["a.yaml is uncovered"], "errors": []}
    assert steps.coverage_annotations(report, "report") == [
        "::warning title=API contract coverage gap::a.yaml is uncovered",
    ]
    assert steps.coverage_annotations(report, "enforce")[0].startswith("::error title=API contract coverage gap::")


def test_coverage_errors_and_missing_report_are_errors():
    assert steps.coverage_annotations({"problems": [], "errors": ["100% broken"]}, "report") == [
        "::error title=API contract coverage error::100%25 broken",
    ]
    assert steps.coverage_annotations(None, "report") == [
        "::error title=API contract coverage::The coverage check did not produce a report",
    ]


def test_annotate_coverage_never_fails_the_step(tmp_path, capsys):
    assert steps.annotate_coverage({"RUNNER_TEMP": str(tmp_path), "MODE": "enforce"}) == 0
    assert "did not produce a report" in capsys.readouterr().out
