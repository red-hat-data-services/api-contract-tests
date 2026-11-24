"""Keep schemas/contract-set.schema.json and the run-time descriptor validator in step.

The commands do not run jsonschema (the reusable workflows use the runner's python3 with
no extra packages). Every command loads the descriptor through openapi-compat's
descriptor.py, which interprets the schema itself. These tests:

- fail if the schema uses a keyword descriptor.py does not implement;
- walk every property in the schema, including array items, additionalProperties and
  conditional (if/then) branches, and generate cases from it: each property omitted,
  given values of the wrong type, and given values that break its const, enum, pattern,
  minLength or minItems rule;
- assert that jsonschema and descriptor.py accept and reject exactly the same cases.

Cases come from the schema, so a property added to it is covered without editing this file.
VALID must exercise every property (test_valid_descriptor_exercises_every_property).

Run with: python3 -m pytest modules/coverage/tests/test_schema.py -v
"""

import copy
import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "schemas" / "contract-set.schema.json").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location(
    "openapi_compat_descriptor", ROOT / "modules" / "openapi-compat" / "descriptor.py")
assert _spec is not None and _spec.loader is not None
descriptor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(descriptor)

SHA = "0" * 40
# Schema-valid, and uses every property the schema declares, with each generator
# adapter used once so both conditional branches are exercised.
VALID = {
    "apiVersion": "contracts.rhoai.io/v1alpha1",
    "kind": "ContractSet",
    "metadata": {"name": "example"},
    "spec": {
        "candidate": {"generator": {
            "adapter": "openapi.bundle.yq-merge", "script": "scripts/bundle.sh", "yqEnvVar": "YQ",
            "sourceDir": "api/openapi/src", "sourceGlobs": ["service.yaml"],
            "entrypointArg": "service.yaml", "output": "api/openapi/service.yaml",
        }},
        "baseline": {"refs": [SHA]},
        "exclusions": [{"path": "api/internal/*", "reason": "internal UI API"}],
        "contracts": [{
            "id": "service-v1",
            "type": "openapi",
            "declaredArtifact": "api/openapi/service.yaml",
            "matchPath": "^/svc/v1(/|$)",
            "generator": {"adapter": "openapi.static", "source": "api/openapi/service.yaml"},
            "baseline": {"refs": [SHA], "reason": "scoped break", "artifact": "api/openapi/old.yaml"},
            "policy": {
                "oasdiffVersion": "1.33.0", "yqVersion": "4.45.1", "flattenAllOf": True,
                "severityOverrides": {"response-optional-property-removed": "warn"},
            },
        }],
    },
}

ABSENT = object()
WRONG_TYPE = {
    "object": [None, [], "", 0, False, "bad"],
    "array": [None, {}, "", 0, False, "bad"],
    "string": [None, 1, 4.45, True, [], {}],
    "boolean": [None, "false", "true", 0, 1, []],
}


def resolve(schema):
    while "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].removeprefix("#/$defs/")]
    return schema


def get(document, path):
    for part in path:
        document = document[part]
    return document


def walk(schema, instance, path, required):
    """Yield (path, schema, required) for every property under ``schema`` that ``instance`` uses."""
    schema = resolve(schema)
    branches = [schema]
    for branch in schema.get("allOf", []):
        if "if" in branch and jsonschema.Draft202012Validator(branch["if"]).is_valid(instance):
            branches.append(branch.get("then", {}))
    if isinstance(instance, dict):
        mandatory = {key for branch in branches for key in branch.get("required", [])}
        for branch in branches:
            properties = branch.get("properties", {})
            for key, child in properties.items():
                if key in instance:
                    yield (*path, key), resolve(child), key in mandatory
                    yield from walk(child, instance[key], (*path, key), key in mandatory)
            extra = branch.get("additionalProperties")
            if isinstance(extra, dict):
                for key in instance:
                    if key not in properties:
                        yield (*path, key), resolve(extra), False
                        yield from walk(extra, instance[key], (*path, key), False)
    if isinstance(instance, list) and "items" in schema and instance:
        # Removing an item is only invalid when the list would drop below minItems.
        needed = schema.get("minItems", 0) >= len(instance)
        yield (*path, 0), resolve(schema["items"]), needed
        yield from walk(schema["items"], instance[0], (*path, 0), needed)


PROPERTIES = list(walk(SCHEMA, VALID, (), True))


def mutations(path, schema, required):
    """(label, value, schema_should_accept) cases for one property."""
    yield "omitted", ABSENT, not required
    for value in WRONG_TYPE.get(schema.get("type"), []):
        yield f"type={value!r}", value, False
    if "const" in schema:
        yield "wrong-const", f"not-{schema['const']}", False
    if "enum" in schema:
        yield "not-in-enum", "not-an-allowed-value", False
    if "pattern" in schema:
        yield "pattern-mismatch", "!! not matching !!", False
    if schema.get("minLength"):
        yield "too-short", "", False
    if schema.get("minItems"):
        yield "too-few-items", [], False


def mutate(path, value):
    document = copy.deepcopy(VALID)
    parent = get(document, path[:-1])
    if value is ABSENT:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    return document


def dotted(path):
    return ".".join(str(part) for part in path) or "<root>"


CASES = [
    pytest.param(path, value, accept, id=f"{dotted(path)}:{label}")
    for path, schema, required in PROPERTIES
    for label, value, accept in mutations(path, schema, required)
]


def schema_accepts(document):
    return jsonschema.Draft202012Validator(SCHEMA).is_valid(document)


def validator_accepts(document):
    try:
        descriptor.validate(document, SCHEMA)
    except descriptor.DescriptorError:
        return False
    return True


def test_schema_is_a_valid_draft_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_validator_implements_every_keyword_the_schema_uses():
    assert sorted(set(descriptor.unsupported_keywords(SCHEMA))) == []


def test_validator_refuses_a_schema_with_an_unknown_keyword(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({**SCHEMA, "properties": {**SCHEMA["properties"], "kind": {"format": "x"}}}))
    with pytest.raises(descriptor.DescriptorError, match="does not implement"):
        descriptor.load_schema(path)


def test_valid_descriptor_exercises_every_property():
    """Every property declared anywhere in the schema appears in VALID, so CASES covers it."""
    declared = set()

    def collect(node, prefix):
        if isinstance(node, dict):
            for key, child in node.get("properties", {}).items():
                declared.add((prefix, key))
                collect(child, prefix)
            for key, value in node.items():
                if key in ("allOf", "items", "then", "additionalProperties"):
                    for child in value if isinstance(value, list) else [value]:
                        collect(child, prefix)
    collect(SCHEMA, "#")
    for name, definition in SCHEMA["$defs"].items():
        collect(definition, name)
    exercised = {path[-1] for path, _, _ in PROPERTIES}
    missing = sorted(key for _, key in declared if key not in exercised)
    assert missing == [], f"add these properties to VALID: {missing}"
    assert schema_accepts(VALID) and validator_accepts(VALID)


@pytest.mark.parametrize("path, value, accept", CASES)
def test_schema_and_validator_agree(path, value, accept):
    document = mutate(path, value)
    assert schema_accepts(document) is accept, "the case's expectation disagrees with jsonschema"
    assert validator_accepts(document) is accept, "descriptor.py disagrees with jsonschema"


def test_contracts_empty_list_is_valid():
    document = mutate(("spec", "contracts"), [])
    assert schema_accepts(document) and validator_accepts(document)


def test_match_path_must_be_a_valid_regex():
    # The one rule stricter than the schema: both commands use matchPath as a regex.
    document = mutate(("spec", "contracts", 0, "matchPath"), "[")
    assert schema_accepts(document)
    with pytest.raises(descriptor.DescriptorError, match="not a valid regular expression"):
        descriptor.validate(document, SCHEMA)


def test_selftest_descriptor_is_valid():
    document = yaml.safe_load((ROOT / "selftest" / "contracts.yaml").read_text(encoding="utf-8"))
    assert schema_accepts(document) and validator_accepts(document)
