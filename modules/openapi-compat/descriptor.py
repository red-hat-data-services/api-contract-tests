"""Validate a ContractSet descriptor against schemas/contract-set.schema.json at run time.

The reusable workflows run on the runner's python3 with no extra packages, so this
module interprets the schema itself instead of depending on jsonschema. It supports
exactly the keywords the schema uses (SUPPORTED_KEYWORDS); ``unsupported_keywords``
reports any other keyword, and validation refuses to run with one, so a schema change
cannot silently go unenforced. modules/coverage/tests/test_schema.py checks that this
module and jsonschema agree on a mutation of every property in the schema.

One rule is stricter than the schema: a contract's ``matchPath`` must compile as a
Python regular expression, because both commands use it as one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "contract-set.schema.json"

# Keywords that constrain a value; each one is implemented in _validate below.
SUPPORTED_KEYWORDS = frozenset({
    "$ref", "type", "const", "enum", "minLength", "pattern", "minItems",
    "required", "properties", "additionalProperties", "items", "allOf", "if", "then",
})
# Keywords that only document the schema.
ANNOTATIONS = frozenset({"$schema", "$id", "$defs", "title", "description", "default"})

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,),
    "integer": (int,), "number": (int, float), "null": (type(None),),
}
_TYPE_NAMES = {
    "object": "a mapping", "array": "a list", "string": "a string", "boolean": "a boolean (true or false)",
    "integer": "an integer", "number": "a number", "null": "null",
}

# Friendlier messages for rules a provider is likely to trip over, keyed by the schema
# location of the offending value (for "required", the location of the missing key).
_REASON = ": record why, for reviewers; an entry without a justification is not accepted"
_HINTS = {
    ("#/properties/spec/properties/contracts", "required"): " (use `contracts: []` for a descriptor with only exclusions)",
    ("#/properties/spec/properties/exclusions/items/properties/reason", "required"): _REASON,
    ("#/properties/spec/properties/exclusions/items/properties/reason", "minLength"): _REASON,
    ("#/$defs/contract/properties/baseline/properties/reason", "required"): _REASON,
    ("#/$defs/contract/properties/baseline/properties/reason", "minLength"): _REASON,
    ("#/$defs/contract/properties/policy/properties/oasdiffVersion", "type"): ' (quote the version, e.g. "1.33.0")',
    ("#/$defs/contract/properties/policy/properties/yqVersion", "type"): ' (quote the version, e.g. "4.45.1")',
    ("#/$defs/baselineRefs/items", "pattern"): " (a full 40- or 64-character Git SHA; branches, tags and HEAD are rejected)",
}


class DescriptorError(ValueError):
    """The descriptor does not match the schema."""


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    unsupported = sorted(set(unsupported_keywords(schema)))
    if unsupported:
        raise DescriptorError(f"{path.name} uses keywords this validator does not implement: {unsupported}")
    return schema


def unsupported_keywords(node: Any) -> Iterator[str]:
    """Every keyword in the schema that is neither enforced nor an annotation."""
    if isinstance(node, list):
        for item in node:
            yield from unsupported_keywords(item)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key not in SUPPORTED_KEYWORDS and key not in ANNOTATIONS:
            yield key
        if key in ("properties", "$defs"):
            for child in value.values():
                yield from unsupported_keywords(child)
        elif key not in ("const", "enum", "required", "default"):
            yield from unsupported_keywords(value)


def validate(descriptor: Any, schema: dict[str, Any] | None = None) -> None:
    """Raise DescriptorError for the first rule the descriptor breaks."""
    root = schema if schema is not None else load_schema()
    _validate(descriptor, root, root, "descriptor", "#")
    for index, contract in enumerate(descriptor["spec"]["contracts"]):
        match_path = contract.get("matchPath")
        if match_path is not None:
            try:
                re.compile(match_path)
            except re.error as error:
                raise DescriptorError(
                    f"spec.contracts[{index}].matchPath {match_path!r} is not a valid regular expression: {error}"
                ) from error


def _child(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return key if path == "descriptor" else f"{path}.{key}"


def _fail(path: str, pointer: str, keyword: str, message: str) -> None:
    raise DescriptorError(f"{path} {message}{_HINTS.get((pointer, keyword), '')}")


def _matches(instance: Any, schema: Any, root: dict[str, Any], path: str, pointer: str) -> bool:
    try:
        _validate(instance, schema, root, path, pointer)
    except DescriptorError:
        return False
    return True


def _validate(instance: Any, schema: dict[str, Any], root: dict[str, Any], path: str, pointer: str) -> None:
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise DescriptorError(f"unsupported $ref {ref!r} in the schema")
        _validate(instance, root["$defs"][ref.removeprefix("#/$defs/")], root, path, ref)

    if "type" in schema:
        expected = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        kinds = tuple(kind for name in expected for kind in _TYPES[name])
        if not isinstance(instance, kinds) or (isinstance(instance, bool) and bool not in kinds):
            names = " or ".join(_TYPE_NAMES[name] for name in expected)
            _fail(path, pointer, "type", f"must be {names}, not {_describe(instance)}")
    if "const" in schema and instance != schema["const"]:
        _fail(path, pointer, "const", f"must be {schema['const']!r}, not {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        _fail(path, pointer, "enum", f"must be one of {', '.join(map(repr, schema['enum']))}, not {instance!r}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            _fail(path, pointer, "minLength", "must not be empty" if schema["minLength"] == 1
                  else f"must be at least {schema['minLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            _fail(path, pointer, "pattern", f"{instance!r} does not match {schema['pattern']!r}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            _fail(path, pointer, "minItems", f"must have at least {schema['minItems']} item(s)")
        if "items" in schema:
            for index, item in enumerate(instance):
                _validate(item, schema["items"], root, _child(path, index), f"{pointer}/items")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                _fail(_child(path, key), f"{pointer}/properties/{key}", "required", "is required")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in instance:
                _validate(instance[key], child, root, _child(path, key), f"{pointer}/properties/{key}")
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, value in instance.items():
                if key not in properties:
                    _validate(value, extra, root, _child(path, key), f"{pointer}/additionalProperties")

    for index, branch in enumerate(schema.get("allOf", [])):
        _validate(instance, branch, root, path, f"{pointer}/allOf/{index}")
    if "if" in schema and "then" in schema and _matches(instance, schema["if"], root, path, f"{pointer}/if"):
        _validate(instance, schema["then"], root, path, f"{pointer}/then")


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"the boolean {str(value).lower()}"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    return f"{type(value).__name__} {value!r}"
