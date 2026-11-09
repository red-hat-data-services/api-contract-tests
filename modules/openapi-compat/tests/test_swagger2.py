"""Tests for Swagger 2.0 support in the openapi-compat engine.

oasdiff reads OpenAPI 3 only, so a Swagger 2.0 document is converted first (by the
swagger2openapi tool built from cmd/swagger2openapi). These tests run the real converter and the
real oasdiff, because the point is that breaking changes are still caught after conversion.

Requires the pinned `oasdiff` (v1.33.0), `yq` (v4.45.1) and the `swagger2openapi` converter, on
PATH or pointed to via OASDIFF, YQ and SWAGGER2OPENAPI. Missing tools are failures, not skips, so
a check that never ran cannot be mistaken for a pass.

Build the converter with:
    go build -C cmd/swagger2openapi -o "$PWD/bin/swagger2openapi" .
Run with: python3 -m pytest modules/openapi-compat/tests/test_swagger2.py -v
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "cmd" / "contract-tests" / "cli.py"

spec = importlib.util.spec_from_file_location("openapi_compat_checker", ROOT / "modules" / "openapi-compat" / "checker.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

OASDIFF_VERSION = "1.33.0"
YQ_VERSION = "4.45.1"

SWAGGER = {
    "swagger": "2.0",
    "info": {"title": "Widgets", "version": "1.0"},
    "host": "api.example.com",
    "basePath": "/api/v1",
    "schemes": ["https"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "paths": {
        "/widgets": {
            "get": {
                "operationId": "listWidgets",
                "parameters": [{"name": "limit", "in": "query", "type": "integer", "format": "int32"}],
                "responses": {
                    "200": {"description": "ok", "schema": {"$ref": "#/definitions/WidgetList"}},
                    "400": {"description": "bad request"},
                },
            },
            "post": {
                "operationId": "createWidget",
                "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Widget"}}],
                "responses": {"201": {"description": "created", "schema": {"$ref": "#/definitions/Widget"}}},
            },
        },
        "/widgets/{id}": {
            "delete": {
                "operationId": "deleteWidget",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "responses": {"204": {"description": "deleted"}},
            },
        },
    },
    "definitions": {
        "Widget": {
            "type": "object",
            "required": ["name"],
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "size": {"type": "integer"}},
        },
        "WidgetList": {"type": "object", "properties": {"items": {"type": "array", "items": {"$ref": "#/definitions/Widget"}}}},
    },
}

DESCRIPTOR = """
apiVersion: contracts.rhoai.io/v1alpha1
kind: ContractSet
metadata:
  name: widgets
spec:
  contracts:
    - id: widgets-v1
      type: openapi
      declaredArtifact: {artifact}
      matchPath: "{match_path}"
      policy:
        oasdiffVersion: "{oasdiff_version}"
        yqVersion: "{yq_version}"
      generator:
        adapter: openapi.static
        source: {artifact}
"""


def mutated(change):
    document = copy.deepcopy(SWAGGER)
    change(document)
    return document


class Provider:
    """A throwaway provider repository whose contract is a Swagger 2.0 document."""

    def __init__(self, work, artifact="api/swagger.json", match_path="^/api/v1(/|$)", document=SWAGGER):
        self.work, self.artifact = work, artifact
        self.repo = work / "provider"
        self.oasdiff = runner.tool_path("oasdiff", os.environ.get("OASDIFF"), ROOT)
        self.yq = runner.tool_path("yq", os.environ.get("YQ"), ROOT)
        self.converter = runner.tool_path("swagger2openapi", os.environ.get("SWAGGER2OPENAPI"), ROOT)
        (self.repo / "api").mkdir(parents=True)
        (self.repo / ".rhoai").mkdir()
        (self.repo / ".rhoai/contracts.yaml").write_text(DESCRIPTOR.format(
            artifact=artifact, match_path=match_path, oasdiff_version=OASDIFF_VERSION, yq_version=YQ_VERSION))
        self.write(document)
        self.git("init", "--initial-branch=main")
        self.git("add", "api", ".rhoai")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", "Accepted baseline")
        self.baseline = self.git("rev-parse", "HEAD").stdout.strip()
        self.calls = 0

    def git(self, *args):
        environment = dict(os.environ, GIT_AUTHOR_NAME="Contract Tests", GIT_COMMITTER_NAME="Contract Tests",
                           GIT_AUTHOR_EMAIL="contract-tests@example.invalid",
                           GIT_COMMITTER_EMAIL="contract-tests@example.invalid",
                           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        return subprocess.run(["git", *args], cwd=self.repo, env=environment, capture_output=True, text=True, check=True)

    def write(self, document, relative=None, text=None):
        path = self.repo / (relative or self.artifact)
        path.write_text(text if text is not None else json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def check(self, enforce=True, converter=True, extra=()):
        self.calls += 1
        output = self.work / f"report-{self.calls}"
        args = ["--repo", str(self.repo), "--contract-set", ".rhoai/contracts.yaml", "--oasdiff", str(self.oasdiff),
                "--yq", str(self.yq), "--baseline-ref", self.baseline, "--output-dir", str(output)]
        if converter:
            args.extend(["--swagger2openapi", str(self.converter)])
        if enforce:
            args.append("--enforce")
        result = subprocess.run([sys.executable, str(CLI), "check", *args, *extra], capture_output=True, text=True, timeout=120)
        assert (output / "report.json").is_file(), result.stderr
        return result, json.loads((output / "report.json").read_text()), output


@pytest.fixture
def provider(tmp_path):
    return Provider(tmp_path)


# ---- the document is converted and compared ---------------------------------------

def test_unchanged_swagger_document_is_compatible_and_the_conversion_is_recorded(provider):
    result, report, output = provider.check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["status"] == "compatible"
    entry = report["baselines"][0]
    assert entry["dialect"] == "swagger-2.0"
    assert entry["converted"]["from"] == "swagger-2.0" and entry["converted"]["to"] == "openapi-3"
    assert entry["converted"]["base_path_prepended"] == "/api/v1"
    assert (output / entry["compare_file"]).is_file()
    assert report["candidate"]["dialect"] == "swagger-2.0"
    assert report["candidate"]["converted"]["sha256"]
    assert report["tool_build"]["swagger2openapi"].startswith("swagger2openapi kin-openapi v")


@pytest.mark.parametrize("name,change,finding", [
    ("an operation is removed", lambda d: d["paths"]["/widgets/{id}"].pop("delete"), "api-removed"),
    ("a required query parameter is added",
     lambda d: d["paths"]["/widgets"]["get"]["parameters"].append(
         {"name": "tenant", "in": "query", "required": True, "type": "string"}), "new-required-request-parameter"),
    ("a body property becomes required", lambda d: d["definitions"]["Widget"]["required"].append("size"),
     "request-property-became-required"),
    ("a response property changes type", lambda d: d["definitions"]["Widget"]["properties"]["size"].update({"type": "string"}),
     "property-type-changed"),
    ("a success response is removed", lambda d: d["paths"]["/widgets"]["get"]["responses"].pop("200"),
     "response-success-status-removed"),
    ("a request media type is removed", lambda d: d.update({"consumes": ["application/xml"]}),
     "request-body-media-type-removed"),
])
def test_breaking_change_is_caught_after_conversion(provider, name, change, finding):
    provider.write(mutated(change))
    result, report, _ = provider.check()
    assert result.returncode == 1, f"{name}: {result.stdout}{result.stderr}"
    assert report["status"] == "breaking"
    assert any(finding in item["id"] for item in report["baselines"][0]["findings"]), (name, report["baselines"][0]["findings"])


@pytest.mark.parametrize("name,change", [
    ("an optional query parameter is added",
     lambda d: d["paths"]["/widgets"]["get"]["parameters"].append({"name": "q", "in": "query", "type": "string"})),
    ("an operation is added",
     lambda d: d["paths"]["/widgets/{id}"].update({"get": {"operationId": "getWidget", "parameters": [
         {"name": "id", "in": "path", "required": True, "type": "string"}], "responses": {"200": {"description": "ok"}}}})),
])
def test_compatible_change_is_not_reported_as_breaking(provider, name, change):
    provider.write(mutated(change))
    result, report, _ = provider.check()
    assert result.returncode == 0, f"{name}: {result.stdout}{result.stderr}"
    assert report["status"] == "compatible"


def test_report_only_mode_records_the_break_without_failing(provider):
    provider.write(mutated(lambda d: d["paths"]["/widgets/{id}"].pop("delete")))
    result, report, _ = provider.check(enforce=False)
    assert result.returncode == 0 and report["status"] == "breaking" and report["mode"] == "report-only"


# ---- basePath ------------------------------------------------------------------------

def test_changed_base_path_is_breaking_although_oasdiff_ignores_servers(provider):
    provider.write(mutated(lambda d: d.update({"basePath": "/api/v2"})))
    result, report, _ = provider.check()
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["status"] == "breaking"
    assert any("removed" in item["id"] for item in report["baselines"][0]["findings"])


def test_match_path_is_matched_against_the_base_path_plus_the_path(tmp_path):
    # The documented paths are /widgets and /widgets/{id}; the URLs are /api/v1/widgets...
    wrong = Provider(tmp_path, match_path="^/widgets(/|$)")
    result, report, _ = wrong.check()
    assert result.returncode == 2
    assert "has no paths matching" in result.stderr


def test_host_and_schemes_are_not_compared(provider):
    # Like servers in an OpenAPI 3 document, a host change is a deployment detail oasdiff does not diff.
    provider.write(mutated(lambda d: d.update({"host": "other.example.com", "schemes": ["http"]})))
    result, report, _ = provider.check()
    assert result.returncode == 0 and report["status"] == "compatible"


def test_document_without_a_base_path_is_converted_unchanged(tmp_path):
    document = mutated(lambda d: d.pop("basePath"))
    document["paths"] = {"/api/v1" + name: item for name, item in document["paths"].items()}
    provider = Provider(tmp_path, document=document)
    result, report, _ = provider.check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["baselines"][0]["converted"]["base_path_prepended"] is None


# ---- formats and dialects -------------------------------------------------------------

def test_yaml_swagger_document_with_an_unquoted_numeric_version(tmp_path):
    text = ("swagger: 2.0\ninfo: {title: Widgets, version: '1'}\nbasePath: /api/v1\n"
            "paths:\n  /widgets:\n    get:\n      responses:\n        '200': {description: ok}\n")
    provider = Provider(tmp_path, artifact="api/swagger.yaml")
    provider.write(None, text=text)
    provider.git("add", "api")
    provider.git("-c", "commit.gpgsign=false", "commit", "-m", "yaml swagger")
    provider.baseline = provider.git("rev-parse", "HEAD").stdout.strip()
    result, report, _ = provider.check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["baselines"][0]["dialect"] == "swagger-2.0"


def test_migrating_a_contract_from_swagger_2_to_openapi_3_is_compatible(provider):
    """Convert the document, commit it as the new artifact, and the check compares across dialects."""
    probe, _, output = provider.check()
    converted = (output / json.loads((output / "report.json").read_text())["baselines"][0]["compare_file"]).read_text()
    provider.write(None, text=converted)
    result, report, _ = provider.check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["baselines"][0]["dialect"] == "swagger-2.0"
    assert report["candidate"]["dialect"] == "openapi-3"
    assert "converted" not in report["candidate"]
    assert report["status"] == "compatible"


# ---- failures are explicit ---------------------------------------------------------------

def test_missing_converter_is_a_clear_error(provider):
    result, report, _ = provider.check(converter=False, extra=["--swagger2openapi", str(provider.work / "nope")])
    assert result.returncode == 2
    assert "swagger2openapi" in result.stderr and "go build" in result.stderr
    assert report["status"] == "error"


def test_dangling_reference_names_the_document_and_the_missing_definition(provider):
    provider.write(mutated(lambda d: d["paths"]["/widgets"]["get"]["responses"]["200"].update(
        {"schema": {"$ref": "#/definitions/Missing"}})))
    result, report, _ = provider.check()
    assert result.returncode == 2
    assert "Cannot convert the Swagger 2.0 document" in result.stderr and "Missing" in result.stderr


def test_openapi_3_documents_do_not_need_the_converter(tmp_path):
    openapi3 = {"openapi": "3.0.3", "info": {"title": "Widgets", "version": "1"},
                "paths": {"/api/v1/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    provider = Provider(tmp_path, document=openapi3)
    result, report, _ = provider.check(converter=False, extra=["--swagger2openapi", str(tmp_path / "nope")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["baselines"][0]["dialect"] == "openapi-3"
    assert "converted" not in report["baselines"][0]
    assert "swagger2openapi" not in report["tool_build"]


# ---- helpers -------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("2.0", True), (2.0, True), (2, True), ("2", True), (" 2.0 ", True),
                                            ("3.0.3", False), ("1.2", False), (None, False)])
def test_is_swagger2(value, expected):
    document = {"swagger": value} if value is not None else {"openapi": "3.0.3"}
    assert runner.is_swagger2(document) is expected
    assert runner.is_swagger2("swagger: 2.0") is False


@pytest.mark.parametrize("base,expected", [("/api/v1", "/api/v1"), ("/api/v1/", "/api/v1"), ("api", "/api"),
                                           ("/", ""), ("", ""), (None, ""), (5, "")])
def test_swagger_base_path(base, expected):
    assert runner.swagger_base_path({"swagger": "2.0", "basePath": base}) == expected


def test_effective_paths():
    swagger = {"swagger": "2.0", "basePath": "/api/v1", "paths": {"/a": {}, "/b/{id}": {}}}
    assert sorted(runner.effective_paths(swagger)) == ["/api/v1/a", "/api/v1/b/{id}"]
    openapi3 = {"openapi": "3.0.3", "basePath": "/ignored", "paths": {"/a": {}}}
    assert runner.effective_paths(openapi3) == ["/a"]
    assert runner.effective_paths({"swagger": "2.0"}) == []
    assert runner.effective_paths({"swagger": "2.0", "paths": []}) == []
    assert runner.effective_paths("not a document") == []


# ---- discover ---------------------------------------------------------------------------------

def test_discover_suggests_a_match_path_that_includes_the_base_path(provider):
    result = subprocess.run([sys.executable, str(CLI), "discover", "--repo", str(provider.repo), "--format", "json"],
                            capture_output=True, text=True, env=dict(os.environ, YQ=str(provider.yq)), timeout=120)
    assert result.returncode == 0, result.stderr
    document = next(d for d in json.loads(result.stdout)["openapi"] if d["path"] == "api/swagger.json")
    assert document["dialect"] == "swagger-2.0"
    assert document["suggested_match_path"].startswith("^/api/v1")
    # And the suggestion really matches what the checker will compare.
    assert runner.document_serves(SWAGGER, document["suggested_match_path"])
