"""Regression tests for the generic CRD compatibility engine."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "modules" / "crd-compat" / "checker.py"
CLI = ROOT / "cmd" / "contract-tests" / "cli.py"

spec = importlib.util.spec_from_file_location("crd_compat_checker", MODULE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


BASE_CRD = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: examples.demo.example.com
spec:
  group: demo.example.com
  names:
    kind: Example
    plural: examples
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                name:
                  type: string
                  default: example
                mode:
                  type: string
                  enum: [fast, slow]
"""


class TestCRDCompatChecker:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.work = tmp_path
        self.repo = self.work / "provider"
        self.repo.mkdir()
        self.crd = self.repo / "config/crd/example.yaml"
        self.crd.parent.mkdir(parents=True)
        self.write(BASE_CRD)
        self.git("init", "--initial-branch=main")
        self.git("add", "config")
        self.commit("baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()

    def write(self, content):
        self.crd.write_text(content, encoding="utf-8")

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
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=environment,
            capture_output=True, text=True, check=True,
        )

    def commit(self, message):
        self.git("commit", "-m", message)

    def check(self, enforce=True, baseline=None):
        output = self.work / f"report-{len(list(self.work.iterdir()))}"
        args = [
            sys.executable, str(CLI), "crd-check",
            "--repo", str(self.repo),
            "--crd-glob", "config/crd/*.yaml",
            "--baseline-ref", baseline or self.baseline,
            "--output-dir", str(output),
        ]
        if enforce:
            args.append("--enforce")
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        report = json.loads((output / "report.json").read_text())
        return result, report

    def test_unchanged_crds_are_compatible(self):
        result, report = self.check()
        assert result.returncode == 0, result.stderr
        assert report["status"] == "compatible"

    def test_removed_property_is_breaking(self):
        self.write(BASE_CRD.replace("                name:\n                  type: string\n                  default: example\n", ""))
        result, report = self.check()
        assert result.returncode == 1
        assert report["status"] == "breaking"
        assert any(item["id"] == "property-removed" for item in report["baselines"][0]["findings"])

    def test_new_required_field_is_breaking(self):
        self.write(BASE_CRD.replace("            spec:\n              type: object\n", "            spec:\n              type: object\n              required: [name]\n"))
        result, report = self.check()
        assert result.returncode == 1
        assert any(item["id"] == "required-property-added" for item in report["baselines"][0]["findings"])

    def test_type_change_is_breaking(self):
        self.write(BASE_CRD.replace("                name:\n                  type: string\n", "                name:\n                  type: integer\n"))
        result, report = self.check()
        assert result.returncode == 1
        assert any(item["id"] == "type-changed" for item in report["baselines"][0]["findings"])

    def test_enum_narrowing_is_breaking(self):
        self.write(BASE_CRD.replace("enum: [fast, slow]", "enum: [fast]"))
        result, report = self.check()
        assert result.returncode == 1
        assert any(item["id"] == "enum-value-removed" for item in report["baselines"][0]["findings"])

    def test_served_version_removal_is_breaking(self):
        self.write(BASE_CRD.replace("      served: true\n", "      served: false\n"))
        result, report = self.check()
        assert result.returncode == 1
        assert any(item["id"] == "crd-version-no-longer-served" for item in report["baselines"][0]["findings"])

    def test_report_only_records_breaking_findings(self):
        self.write(BASE_CRD.replace("enum: [fast, slow]", "enum: [fast]"))
        result, report = self.check(enforce=False)
        assert result.returncode == 0
        assert report["status"] == "breaking"
        assert report["baselines"][0]["findings"]

    def test_mutable_baseline_is_rejected(self):
        result, report = self.check(baseline="main")
        assert result.returncode == 2
        assert report["status"] == "error"
        assert "full 40- or 64-character Git SHA" in report["errors"][0]

    def early_commit(self):
        """A commit from before the provider had CRDs."""
        tree = self.git("hash-object", "-t", "tree", os.devnull).stdout.strip()
        return self.git("commit-tree", tree, "-m", "before the CRDs existed").stdout.strip()

    def test_crds_absent_from_the_baseline_are_new_not_a_failure(self):
        result, report = self.check(baseline=self.early_commit())
        assert result.returncode == 0, result.stdout + result.stderr
        assert report["status"] == "new"
        assert report["baselines"][0]["status"] == "new"

    def test_new_crd_next_to_existing_ones_is_compatible(self):
        extra = self.repo / "config/crd/another.yaml"
        extra.write_text(BASE_CRD.replace("examples", "gadgets").replace("Example", "Gadget"), encoding="utf-8")
        result, report = self.check()
        assert result.returncode == 0, result.stdout + result.stderr
        assert report["status"] == "compatible"

    def test_identical_copies_of_a_crd_are_accepted(self):
        # Providers may ship the same CRD in several kustomize overlays.
        copy = self.repo / "config/crd/example-copy.yaml"
        copy.write_text(BASE_CRD, encoding="utf-8")
        self.git("add", "config")
        self.commit("ship a second, identical copy")
        result, report = self.check(baseline=self.git("rev-parse", "HEAD").stdout.strip())
        assert result.returncode == 0, result.stdout + result.stderr
        assert report["status"] == "compatible"

    def test_divergent_copies_in_the_candidate_are_an_error(self):
        # Without this, a breaking change in one copy could hide behind the other.
        copy = self.repo / "config/crd/example-copy.yaml"
        copy.write_text(BASE_CRD.replace("enum: [fast, slow]", "enum: [fast]"), encoding="utf-8")
        result, report = self.check()
        assert result.returncode == 2
        assert report["status"] == "error"
        error = "".join(report["errors"])
        assert "demo.example.com/Example is defined differently" in error
        assert "config/crd/example.yaml" in error and "config/crd/example-copy.yaml" in error

    def test_divergent_copies_in_the_baseline_are_an_error(self):
        copy = self.repo / "config/crd/example-copy.yaml"
        copy.write_text(BASE_CRD.replace("enum: [fast, slow]", "enum: [fast]"), encoding="utf-8")
        self.git("add", "config")
        self.commit("diverging copy")
        diverging = self.git("rev-parse", "HEAD").stdout.strip()
        copy.write_text(BASE_CRD, encoding="utf-8")
        result, report = self.check(baseline=diverging)
        assert result.returncode == 2
        assert f"{diverging}:config/crd/example-copy.yaml" in "".join(report["errors"])

    def test_glob_that_misses_the_baseline_crds_is_still_an_error(self):
        # The baseline has a CRD, just not where the glob looks: a configuration error.
        moved = self.repo / "deploy"
        moved.mkdir()
        self.git("mv", "config/crd/example.yaml", "deploy/example.yaml")
        self.commit("move the CRD")
        elsewhere = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("mv", "deploy/example.yaml", "config/crd/example.yaml")
        self.commit("move it back")
        result, report = self.check(baseline=elsewhere)
        assert result.returncode == 2
        assert "glob is probably wrong" in "".join(report["errors"])
