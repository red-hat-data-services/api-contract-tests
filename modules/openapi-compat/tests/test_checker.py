"""Regression tests for the generic openapi-compat engine.

These use a synthetic fixture repository (not any real provider's sources)
so the module's own test suite stays provider-agnostic. The provider-side
regression test that exercises this engine against the real Model Catalog
sources and bundler lives in the model-registry repository
(scripts/test_catalog_contract_compat.py).

Requires real pinned `oasdiff` (v1.33.0) and `yq` (v4.45.1) on PATH, or
pointed to via the OASDIFF/YQ environment variables. Missing tools are
failures, not skips, so a check that never ran cannot be mistaken for a pass.

Run with: python3 -m pytest modules/openapi-compat/tests/test_checker.py -v
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "modules" / "openapi-compat"
CLI = ROOT / "cmd" / "contract-tests" / "cli.py"

spec = importlib.util.spec_from_file_location("openapi_compat_checker", MODULE_DIR / "checker.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

OASDIFF_VERSION = "1.33.0"
YQ_VERSION = "4.45.1"

BUNDLE_SCRIPT = """#!/bin/bash
set -e
cd "$(pwd)/$(dirname "$0")/.."
: "${YQ:?YQ must be set}"
NAME="${1%.yaml}"
OUT_FILE="api/openapi/${NAME}.yaml"
"$YQ" eval-all '. as $item ireduce ({}; . * $item)' \\
  api/openapi/src/service-base.yaml api/openapi/src/ext/*.yaml >"$OUT_FILE"
"""

BASE_SPEC = """
openapi: 3.0.3
info:
  title: Example Service
  version: "1"
paths:
  /svc/v1/items:
    get:
      operationId: listItems
      parameters:
        - name: pageSize
          in: query
          schema: {type: integer}
      responses:
        "200":
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ItemPage'
components:
  schemas:
    ItemPage:
      type: object
      properties:
        items:
          type: array
          items:
            $ref: '#/components/schemas/Item'
    Item:
      type: object
      required: [id, name]
      properties:
        id: {type: string}
        name: {type: string}
        sourceId: {type: string}
"""

EXT_SPEC = """
paths:
  /other/v1/widgets:
    get:
      operationId: listWidgets
      responses:
        "200":
          description: OK
"""

DESCRIPTOR = """
apiVersion: contracts.rhoai.io/v1alpha1
kind: ContractSet
metadata:
  name: example-service
spec:
  contracts:
    - id: example-service-v1
      type: openapi
      declaredArtifact: api/openapi/service.yaml
      matchPath: "^/svc/v1(/|$)"
      policy:
        oasdiffVersion: "{oasdiff_version}"
        yqVersion: "{yq_version}"
        flattenAllOf: true
        severityOverrides:
          response-optional-property-removed: warn
      generator:
        adapter: openapi.bundle.yq-merge
        script: scripts/bundle.sh
        yqEnvVar: YQ
        sourceDir: api/openapi/src
        sourceGlobs: ["service-base.yaml", "ext/*.yaml"]
        entrypointArg: service.yaml
        output: api/openapi/service.yaml
""".format(oasdiff_version=OASDIFF_VERSION, yq_version=YQ_VERSION)


class TestOpenAPICompatChecker:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.oasdiff = runner.tool_path("oasdiff", os.environ.get("OASDIFF"), ROOT)
        self.yq = runner.tool_path("yq", os.environ.get("YQ"), ROOT)
        self.work = tmp_path
        self.repo = self.work / "provider"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "api/openapi/src/ext").mkdir(parents=True)
        (self.repo / "scripts/bundle.sh").write_text(BUNDLE_SCRIPT, encoding="utf-8")
        os.chmod(self.repo / "scripts/bundle.sh", 0o755)
        (self.repo / "api/openapi/src/service-base.yaml").write_text(BASE_SPEC, encoding="utf-8")
        (self.repo / "api/openapi/src/ext/other.yaml").write_text(EXT_SPEC, encoding="utf-8")
        (self.repo / ".rhoai").mkdir()
        (self.repo / ".rhoai/contracts.yaml").write_text(DESCRIPTOR, encoding="utf-8")
        self.regenerate()
        self.git("init", "--initial-branch=main")
        self.git("add", "api", "scripts", ".rhoai")
        self.commit("Accepted service baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.calls = 0

    def git(self, *args):
        environment = dict(
            os.environ,
            GIT_AUTHOR_NAME="Contract Tests",
            GIT_COMMITTER_NAME="Contract Tests",
            GIT_AUTHOR_EMAIL="contract-tests@example.invalid",
            GIT_COMMITTER_EMAIL="contract-tests@example.invalid",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
        )
        return subprocess.run(["git", *args], cwd=self.repo, env=environment,
                              capture_output=True, text=True, check=True)

    def commit(self, message):
        self.git("-c", "commit.gpgsign=false", "commit", "-m", message)

    def mutate(self, expression, relative="api/openapi/src/service-base.yaml"):
        runner.command([self.yq, "eval", "-i", expression, self.repo / relative], cwd=self.repo)

    def regenerate(self):
        runner.adapters.generate(
            "openapi.bundle.yq-merge", self.repo,
            {"script": "scripts/bundle.sh", "sourceDir": "api/openapi/src",
             "sourceGlobs": ["service-base.yaml", "ext/*.yaml"],
             "entrypointArg": "service.yaml", "output": "api/openapi/service.yaml", "yqEnvVar": "YQ"},
            self.yq, self.repo / "api/openapi/service.yaml",
        )

    def check(self, refs=None, enforce=True, contract_id=None, extra=()):
        self.calls += 1
        output = self.work / f"report-{self.calls}"
        args = ["--repo", str(self.repo), "--contract-set", ".rhoai/contracts.yaml",
                "--oasdiff", str(self.oasdiff), "--yq", str(self.yq), "--output-dir", str(output)]
        for ref in refs if refs is not None else [self.baseline]:
            args.extend(["--baseline-ref", ref])
        if contract_id:
            args.extend(["--contract-id", contract_id])
        if enforce:
            args.append("--enforce")
        result = subprocess.run([sys.executable, str(CLI), "check", *args, *extra],
                                capture_output=True, text=True, timeout=120)
        assert (output / "report.json").is_file(), result.stderr
        return result, json.loads((output / "report.json").read_text()), output

    def assert_breaking(self, expression, check_id, relative="api/openapi/src/service-base.yaml"):
        self.mutate(expression, relative)
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 1, result.stdout + result.stderr
        assert report["status"] == "breaking"
        assert any(check_id in item["id"] for item in report["baselines"][0]["findings"]), report

    def test_unchanged_contract_and_saved_baseline(self):
        before = (self.repo / "api/openapi/service.yaml").read_bytes()
        result, report, output = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"
        assert report["contract_id"] == "example-service-v1"
        baseline = report["baselines"][0]
        assert baseline["sha"] == self.baseline
        assert (output / baseline["file"]).read_bytes() == before
        assert baseline["sha256"] == report["candidate"]["sha256"]
        assert (self.repo / "api/openapi/service.yaml").read_bytes() == before

    def test_optional_response_field_is_compatible(self):
        self.mutate('.components.schemas.Item.properties.supportUrl = {"type": "string"}')
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"

    def test_removed_operation_is_breaking(self):
        self.assert_breaking('del(.paths."/svc/v1/items".get)', "api-removed")

    def test_removed_response_field_is_breaking_via_severity_override(self):
        self.assert_breaking('del(.components.schemas.Item.properties.sourceId)', "response-optional-property-removed")

    def test_changed_response_type_is_breaking(self):
        self.assert_breaking('.components.schemas.Item.properties.sourceId.type = "integer"', "response-property-type-changed")

    def test_new_required_input_is_breaking(self):
        self.assert_breaking(
            '.paths."/svc/v1/items".get.parameters += [{"name": "tenant", "in": "query", "required": true, "schema": {"type": "string"}}]',
            "required",
        )

    def test_report_only_still_records_break(self):
        self.mutate('del(.components.schemas.Item.properties.sourceId)')
        self.regenerate()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 0, result.stderr
        assert report["status"] == "breaking"
        assert report["baselines"][0]["findings"]

    def test_stale_bundle_fails_even_in_report_only_mode(self):
        self.mutate('.components.schemas.Item.properties.supportUrl = {"type": "string"}')
        before = (self.repo / "api/openapi/service.yaml").read_bytes()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"
        assert "Stale committed" in report["errors"][0]
        assert (self.repo / "api/openapi/service.yaml").read_bytes() == before

    def test_committing_source_and_bundle_cannot_move_pinned_baseline(self):
        self.mutate('del(.components.schemas.Item.properties.sourceId)')
        self.regenerate()
        self.git("add", "api")
        self.commit("Candidate changes source and generated contract together")
        result, report, _ = self.check()
        assert result.returncode == 1, result.stderr
        assert report["baselines"][0]["sha"] == self.baseline
        assert report["candidate"]["head"] != self.baseline

    def test_every_baseline_is_checked(self):
        self.mutate('del(.components.schemas.Item.properties.sourceId)')
        self.regenerate()
        self.git("add", "api")
        self.commit("Another baseline")
        newer = self.git("rev-parse", "HEAD").stdout.strip()
        result, report, _ = self.check(refs=[newer, self.baseline])
        assert result.returncode == 1, result.stderr
        assert report["baselines"][0]["compatible"]
        assert not report["baselines"][1]["compatible"]

    def test_missing_baseline_fails_in_report_only_mode(self):
        result, report, _ = self.check(refs=["0" * 40], enforce=False)
        assert result.returncode == 2
        assert "Cannot resolve baseline" in report["errors"][0]

    def test_mutable_baseline_reference_is_rejected(self):
        result, report, _ = self.check(refs=["main"], enforce=False)
        assert result.returncode == 2
        assert "full 40- or 64-character Git SHA" in report["errors"][0]

    def test_baseline_with_no_matching_paths_fails(self):
        # A baseline whose declared artifact exists but has no paths matching
        # this contract's matchPath is the generalized "wrong API/version" case.
        (self.repo / "api/openapi/other-service.yaml").write_text(EXT_SPEC, encoding="utf-8")
        self.git("add", "api")
        self.commit("Unrelated artifact, not a service-v1 baseline")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        descriptor = DESCRIPTOR.replace("api/openapi/service.yaml", "api/openapi/other-service.yaml")
        (self.repo / ".rhoai/contracts-wrong.yaml").write_text(descriptor, encoding="utf-8")
        result, report, _ = self.check(refs=[ref], extra=["--contract-set", ".rhoai/contracts-wrong.yaml"])
        assert result.returncode == 2
        assert "wrong API/version" in report["errors"][0]

    def test_malformed_source_fails_generation(self):
        (self.repo / "api/openapi/src/service-base.yaml").write_text("paths: [\n", encoding="utf-8")
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"

    def test_unresolved_reference_fails_validation(self):
        self.mutate('.paths."/svc/v1/items".get.responses."200".content."application/json".schema."$ref" = "#/components/schemas/DoesNotExist"')
        self.regenerate()
        result, report, _ = self.check(enforce=False)
        assert result.returncode == 2
        assert report["status"] == "error"

    def test_change_outside_match_path_is_out_of_scope(self):
        self.mutate('del(.paths."/other/v1/widgets".get)', relative="api/openapi/src/ext/other.yaml")
        self.regenerate()
        result, report, _ = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"
        assert report["baselines"][0]["sha256"] != report["candidate"]["sha256"]

    def test_local_oasdiff_configuration_cannot_suppress_findings(self):
        (self.repo / ".oasdiff.yaml").write_text('match-path: "^/nothing$"\n')
        self.assert_breaking('del(.components.schemas.Item.properties.sourceId)', "response-optional-property-removed")

    def test_missing_tool_is_not_a_skipped_check(self):
        result, report, _ = self.check(extra=["--oasdiff", str(self.work / "missing-oasdiff")])
        assert result.returncode == 2
        assert "Missing executable oasdiff" in report["errors"][0]

    def test_existing_report_directory_is_not_overwritten(self):
        _, _, output = self.check()
        before = (output / "report.json").read_bytes()
        result = subprocess.run([sys.executable, str(CLI), "check", "--repo", str(self.repo),
                                 "--contract-set", ".rhoai/contracts.yaml",
                                 "--baseline-ref", self.baseline, "--output-dir", str(output)],
                                capture_output=True, text=True)
        assert result.returncode == 2
        assert (output / "report.json").read_bytes() == before

    def test_unregistered_generator_adapter_fails_closed(self):
        descriptor = DESCRIPTOR.replace("openapi.bundle.yq-merge", "openapi.bundle.totally-made-up")
        (self.repo / ".rhoai/contracts.yaml").write_text(descriptor, encoding="utf-8")
        self.git("add", ".rhoai")
        self.commit("Point at an unregistered adapter")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        result, report, _ = self.check(refs=[ref], enforce=False)
        assert result.returncode == 2
        assert "Unregistered generator adapter" in report["errors"][0]

    def test_static_adapter_copies_declared_source_as_candidate(self):
        static_descriptor = DESCRIPTOR.replace(
            """generator:
        adapter: openapi.bundle.yq-merge
        script: scripts/bundle.sh
        yqEnvVar: YQ
        sourceDir: api/openapi/src
        sourceGlobs: ["service-base.yaml", "ext/*.yaml"]
        entrypointArg: service.yaml
        output: api/openapi/service.yaml""",
            """generator:
        adapter: openapi.static
        source: api/openapi/service.yaml""",
        )
        (self.repo / ".rhoai/contracts.yaml").write_text(static_descriptor, encoding="utf-8")
        self.git("add", ".rhoai")
        self.commit("Switch to the static adapter")
        ref = self.git("rev-parse", "HEAD").stdout.strip()
        result, report, _ = self.check(refs=[ref])
        assert result.returncode == 0, result.stdout + result.stderr
        assert report["status"] == "compatible"

    @pytest.mark.parametrize("code,data", [(102, "[]"), (1, "[]"), (0, "not-json"),
                                          (0, "{}"), (0, '[{"id": "incomplete"}]')])
    def test_tool_failure_or_malformed_output_is_not_compatibility_success(self, code, data):
        result = subprocess.CompletedProcess([], code, data, "tool error")
        with pytest.raises(runner.CheckError):
            runner.findings(result, "breaking")

    def test_wrong_tool_version_is_rejected(self):
        with mock.patch.object(runner, "command", side_effect=[
            subprocess.CompletedProcess([], 0, "oasdiff version 1.0.0\n", ""),
            subprocess.CompletedProcess([], 0, "\tmod github.com/oasdiff/oasdiff v1.0.0 hash\n", ""),
        ]):
            with pytest.raises(runner.CheckError):
                runner.verify_oasdiff(self.oasdiff, OASDIFF_VERSION, self.work)

    def test_wrong_yq_version_is_rejected(self):
        with mock.patch.object(runner, "command", side_effect=[
            subprocess.CompletedProcess([], 0, "yq version v0.0.0\n", ""),
            subprocess.CompletedProcess([], 0, "\tmod github.com/mikefarah/yq/v4 v0.0.0 hash\n", ""),
        ]):
            with pytest.raises(runner.CheckError):
                runner.verify_yq(self.yq, YQ_VERSION, self.work)
