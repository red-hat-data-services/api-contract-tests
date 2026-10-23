"""Regression tests for the coverage module.

These build a synthetic provider repository so the suite stays
provider-agnostic. Like the other modules they need the pinned `yq` (v4.45.1)
on PATH or pointed to by the YQ environment variable; a missing tool is a
failure, not a skip.

Run with: python3 -m pytest modules/coverage/tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "cmd" / "contract-tests" / "cli.py"

spec = importlib.util.spec_from_file_location("coverage_checker", ROOT / "modules" / "coverage" / "checker.py")
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)

YQ_VERSION = "4.45.1"

SPEC = """
openapi: 3.0.3
info:
  title: {title}
  version: "1"
paths:
  {prefix}/items:
    get:
      responses:
        "200": {{description: OK}}
  {prefix}/items/{{id}}:
    get:
      responses:
        "200": {{description: OK}}
"""

CRD = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: widgets.example.io}
spec:
  group: example.io
  names: {kind: Widget}
  versions: []
"""

DESCRIPTOR = """
apiVersion: contracts.rhoai.io/v1alpha1
kind: ContractSet
metadata:
  name: example
spec:
{exclusions}  contracts:
    - id: service-v1
      type: openapi
      declaredArtifact: api/openapi/service.yaml
      matchPath: "^/svc/v1(/|$)"
      policy:
        oasdiffVersion: "1.33.0"
        yqVersion: "{yq_version}"
      generator:
        adapter: openapi.bundle.yq-merge
        script: scripts/bundle.sh
        sourceDir: api/openapi/src
        sourceGlobs: ["service.yaml", "lib/*.yaml"]
        entrypointArg: service.yaml
        output: api/openapi/service.yaml
"""


class TestCoverage:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.yq = coverage.openapi.tool_path("yq", os.environ.get("YQ"), ROOT)
        self.repo = tmp_path / "provider"
        self.repo.mkdir()
        self.write("api/openapi/service.yaml", SPEC.format(title="Service", prefix="/svc/v1"))
        self.write("api/openapi/src/service.yaml", SPEC.format(title="Service", prefix="/svc/v1"))
        self.write("api/openapi/src/lib/common.yaml", "components:\n  schemas: {}\n")
        self.write("scripts/bundle.sh", "#!/bin/bash\n# bundles openapi with $YQ\n")
        self.descriptor()
        self.git("init", "--initial-branch=main")
        self.git("add", ".")

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def descriptor(self, exclusions="", yq_version=YQ_VERSION):
        self.write(".rhoai/contracts.yaml", DESCRIPTOR.format(exclusions=exclusions, yq_version=yq_version))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True,
                              env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull))

    def run(self, command="coverage", *extra):
        result = subprocess.run(
            [sys.executable, str(CLI), command, "--repo", str(self.repo), "--yq", str(self.yq),
             "--format", "json", *extra],
            capture_output=True, text=True, timeout=120,
        )
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = None
        return result, report

    def statuses(self, report):
        return {row["path"]: row["status"] for row in report["openapi"]}

    def test_fully_registered_repository_is_complete(self):
        result, report = self.run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert report["status"] == "complete"
        assert self.statuses(report) == {
            "api/openapi/service.yaml": "contract",
            "api/openapi/src/service.yaml": "source",
        }
        assert report["problems"] == []

    def test_unregistered_document_fails_and_report_only_does_not(self):
        self.write("api/openapi/other.yaml", SPEC.format(title="Other", prefix="/other/v1"))
        result, report = self.run()
        assert result.returncode == 1
        assert report["status"] == "gaps"
        assert self.statuses(report)["api/openapi/other.yaml"] == "uncovered"
        assert any("api/openapi/other.yaml" in text for text in report["problems"])
        result, report = self.run("coverage", "--report-only")
        assert result.returncode == 0
        assert report["status"] == "gaps"

    def test_untracked_spec_is_found_but_ignored_files_are_not(self):
        self.write(".gitignore", "build/\n")
        self.write("build/generated.yaml", SPEC.format(title="Built", prefix="/b"))
        self.write("api/openapi/new.yaml", SPEC.format(title="New", prefix="/new/v1"))
        _, report = self.run()
        statuses = self.statuses(report)
        assert statuses["api/openapi/new.yaml"] == "uncovered"
        assert "build/generated.yaml" not in statuses

    def test_json_documents_are_detected_and_other_json_is_not(self):
        self.write("clients/swagger.json", json.dumps({"swagger": "2.0", "paths": {}}))
        self.write("package.json", json.dumps({"name": "x", "version": "1.0.0"}))
        _, report = self.run()
        statuses = self.statuses(report)
        assert statuses["clients/swagger.json"] == "uncovered"
        assert "package.json" not in statuses

    def test_exclusion_with_reason_covers_a_document(self):
        self.write("clients/swagger.yaml", SPEC.format(title="Internal", prefix="/internal"))
        self.descriptor(exclusions="  exclusions:\n    - path: clients/*.yaml\n      reason: UI internal API, not a provider contract\n")
        result, report = self.run()
        assert result.returncode == 0, result.stdout
        row = next(item for item in report["openapi"] if item["path"] == "clients/swagger.yaml")
        assert row["status"] == "excluded"
        assert "UI internal" in row["reason"]

    def test_exclusion_without_reason_is_an_error(self):
        self.write("clients/swagger.yaml", SPEC.format(title="Internal", prefix="/internal"))
        self.descriptor(exclusions="  exclusions:\n    - path: clients/*.yaml\n")
        result, _ = self.run()
        assert result.returncode == 2
        assert "needs a 'reason'" in result.stderr

    def test_stale_exclusion_is_a_warning_not_a_failure(self):
        self.descriptor(exclusions="  exclusions:\n    - path: gone/*.yaml\n      reason: removed long ago\n")
        result, report = self.run()
        assert result.returncode == 0
        assert any("gone/*.yaml" in text for text in report["warnings"])

    def test_dangling_references_fail(self):
        self.git("rm", "-q", "-f", "api/openapi/service.yaml")
        self.git("rm", "-q", "-f", "api/openapi/src/lib/common.yaml")
        result, report = self.run()
        assert result.returncode == 1
        joined = "\n".join(report["problems"])
        assert "declaredArtifact api/openapi/service.yaml is not an OpenAPI document" in joined
        assert "sourceGlob 'lib/*.yaml' matches no files" in joined

    def test_wrong_pinned_yq_version_fails_closed(self):
        self.descriptor(yq_version="4.0.0")
        result, report = self.run()
        assert result.returncode == 2
        assert report["status"] == "error"

    def test_crd_coverage_requires_the_glob_to_match(self):
        self.write("config/crd/widgets.yaml", CRD)
        self.write("config/overlays/extra.yaml", CRD.replace("widgets", "gadgets").replace("Widget", "Gadget"))
        _, report = self.run()
        assert report["crd_manifests_found"] == 2 and report["crds"] == []
        result, report = self.run("coverage", "--crd-glob", "config/crd/*.yaml")
        assert result.returncode == 1
        assert {row["path"]: row["status"] for row in report["crds"]} == {
            "config/crd/widgets.yaml": "covered", "config/overlays/extra.yaml": "uncovered",
        }
        result, report = self.run("coverage", "--crd-glob", "config/*.yaml")
        assert result.returncode == 0, result.stdout
        result, report = self.run("coverage", "--crd-glob", "config/*.yaml", "--crd-glob", "nowhere/*.yaml")
        assert result.returncode == 1
        assert any("'nowhere/*.yaml' matches no CustomResourceDefinition" in text for text in report["problems"])

    def test_path_scope_reports_paths_outside_match_path(self):
        self.write("api/openapi/service.yaml", SPEC.format(title="Service", prefix="/svc/v1")
                   + "  /plugin/v1/things:\n    get:\n      responses:\n        \"200\": {description: OK}\n")
        _, report = self.run()
        scope = report["path_scope"][0]
        assert scope["paths"] == 3 and scope["outside_match_path"] == 1
        assert scope["examples"] == ["/plugin/v1/things"]

    def test_discover_classifies_documents_and_suggests_shapes(self):
        self.write("clients/openapi.yaml", SPEC.format(title="Hand written", prefix="/hand/v1"))
        result, report = self.run("discover")
        assert result.returncode == 0, result.stderr
        by_path = {item["path"]: item for item in report["openapi"]}
        bundled = by_path["api/openapi/service.yaml"]
        assert bundled["kind"] == "bundled"
        assert bundled["suggested_match_path"] == "^/svc/v1(/|$)"
        assert bundled["suggested_generator"] == {
            "adapter": "openapi.bundle.yq-merge", "sourceDir": "api/openapi/src",
            "entrypointArg": "service.yaml", "output": "api/openapi/service.yaml",
        }
        assert by_path["api/openapi/src/service.yaml"]["kind"] == "source"
        standalone = by_path["clients/openapi.yaml"]
        assert standalone["kind"] == "standalone"
        assert standalone["suggested_generator"] == {"adapter": "openapi.static", "source": "clients/openapi.yaml"}
        assert report["bundler_script_candidates"] == ["scripts/bundle.sh"]
        assert report["descriptor_present"] is True

    def test_discover_needs_no_descriptor(self):
        self.git("rm", "-q", "-f", ".rhoai/contracts.yaml")
        result, report = self.run("discover")
        assert result.returncode == 0
        assert report["descriptor_present"] is False

    def test_kustomize_patches_are_not_crd_manifests(self):
        self.write("config/crd/widgets.yaml", CRD)
        self.write("config/crd/patches/webhook.yaml", """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.io
spec:
  conversion:
    strategy: Webhook
""")
        result, report = self.run("coverage", "--crd-glob", "config/crd/*.yaml")
        assert result.returncode == 0, result.stdout
        assert [row["path"] for row in report["crds"]] == ["config/crd/widgets.yaml"]

    def test_crd_exclude_covers_deliberate_test_data_and_flags_stale_patterns(self):
        self.write("config/crd/widgets.yaml", CRD)
        self.write("testdata/mock.yaml", CRD.replace("widgets", "mocks").replace("Widget", "Mock"))
        result, report = self.run("coverage", "--crd-glob", "config/crd/*.yaml")
        assert result.returncode == 1
        result, report = self.run("coverage", "--crd-glob", "config/crd/*.yaml",
                                  "--crd-exclude", "testdata/*", "--crd-exclude", "gone/*")
        assert result.returncode == 0, result.stdout
        assert {row["path"]: row["status"] for row in report["crds"]}["testdata/mock.yaml"] == "excluded"
        assert any("'gone/*'" in text for text in report["warnings"])

    def test_crd_only_repository_needs_no_descriptor(self):
        for relative in ("api/openapi/service.yaml", "api/openapi/src/service.yaml", ".rhoai/contracts.yaml"):
            self.git("rm", "-q", "-f", relative)
        self.write("config/crd/widgets.yaml", CRD)
        result, report = self.run("coverage", "--crd-glob", "config/crd/*.yaml")
        assert result.returncode == 0, result.stdout + result.stderr
        assert report["status"] == "complete" and report["openapi"] == []
        assert any("only CRD coverage" in text for text in report["warnings"])
        result, report = self.run("coverage")
        assert result.returncode == 2

    def test_missing_descriptor_with_openapi_documents_is_an_error(self):
        self.git("rm", "-q", "-f", ".rhoai/contracts.yaml")
        result, _ = self.run("coverage", "--crd-glob", "config/crd/*.yaml")
        assert result.returncode == 2
        assert "Descriptor not found" in result.stderr
