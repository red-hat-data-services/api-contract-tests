"""Keep schemas/contract-set.schema.json and the checkers' descriptor rules in step.

The checkers do not validate against the schema at runtime, so these tests pin the
shapes both must agree on: ``contracts: []`` is the only exclusion-only descriptor,
and an omitted or null ``contracts`` (or ``spec``) is invalid.

Run with: python3 -m pytest modules/coverage/tests/test_schema.py -v
"""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "schemas" / "contract-set.schema.json").read_text(encoding="utf-8"))

EXCLUSION_ONLY = """\
apiVersion: contracts.rhoai.io/v1alpha1
kind: ContractSet
metadata:
  name: example
spec:
{contracts}  exclusions:
    - path: api/openapi/*
      reason: SDK definitions generated from the CRDs, not a servable API
"""


def errors(document):
    validator = jsonschema.Draft202012Validator(SCHEMA)
    return [error.message for error in validator.iter_errors(document)]


def test_schema_is_a_valid_draft_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_selftest_descriptor_is_valid():
    document = yaml.safe_load((ROOT / "selftest" / "contracts.yaml").read_text(encoding="utf-8"))
    assert errors(document) == []


def test_exclusion_only_descriptor_with_an_empty_contracts_list_is_valid():
    assert errors(yaml.safe_load(EXCLUSION_ONLY.format(contracts="  contracts: []\n"))) == []


@pytest.mark.parametrize("contracts", ["  contracts:\n", "  contracts: null\n", "", "  contracts: {id: x}\n"],
                         ids=["no-value", "null", "omitted", "mapping"])
def test_contracts_must_be_a_list(contracts):
    # Mirrors modules/coverage/checker.py:descriptor_contracts, which rejects the same shapes.
    assert errors(yaml.safe_load(EXCLUSION_ONLY.format(contracts=contracts)))


@pytest.mark.parametrize("exclusions", ["  exclusions:\n", "  exclusions: null\n", "  exclusions: {path: x, reason: y}\n"],
                         ids=["no-value", "null", "mapping"])
def test_exclusions_must_be_a_list(exclusions):
    # Mirrors modules/coverage/checker.py:load_exclusions; an omitted list is valid.
    head = EXCLUSION_ONLY.split("  exclusions:")[0].format(contracts="  contracts: []\n")
    assert errors(yaml.safe_load(head + exclusions))
    assert errors(yaml.safe_load(head)) == []


@pytest.mark.parametrize("spec", ["spec:\n", "spec: []\n", ""], ids=["no-value", "list", "omitted"])
def test_spec_must_be_a_mapping(spec):
    # Mirrors modules/openapi-compat/checker.py:load_descriptor.
    document = yaml.safe_load(
        "apiVersion: contracts.rhoai.io/v1alpha1\nkind: ContractSet\nmetadata:\n  name: example\n" + spec)
    assert errors(document)
