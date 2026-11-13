"""Keep schemas/contract-set.schema.json and the shared descriptor validator in step.

The commands do not run jsonschema at runtime (the reusable workflows use the runner's
python3 with no extra packages). Instead every command loads the descriptor through
openapi-compat's ``validate_descriptor``, which checks the shapes the commands
dereference. The table below mutates one field of a schema-valid descriptor at a time
and asserts that jsonschema and ``validate_descriptor`` agree on every case.

Semantic rules (full Git SHAs, reasons, registered adapters, exclusion entries) are
outside ``validate_descriptor`` by design and are covered by the command tests.

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

_spec = importlib.util.spec_from_file_location("openapi_compat_checker", ROOT / "modules" / "openapi-compat" / "checker.py")
assert _spec is not None and _spec.loader is not None
openapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(openapi)

SHA = "0" * 40
VALID = {
    "apiVersion": "contracts.rhoai.io/v1alpha1",
    "kind": "ContractSet",
    "metadata": {"name": "example"},
    "spec": {
        "candidate": {"generator": {"adapter": "openapi.static", "source": "api/openapi/service.yaml"}},
        "baseline": {"refs": [SHA]},
        "exclusions": [{"path": "api/internal/*", "reason": "internal UI API"}],
        "contracts": [{
            "id": "service-v1",
            "type": "openapi",
            "declaredArtifact": "api/openapi/service.yaml",
            "policy": {"oasdiffVersion": "1.33.0", "yqVersion": "4.45.1",
                       "severityOverrides": {"response-optional-property-removed": "warn"}},
            "generator": {"adapter": "openapi.static", "source": "api/openapi/service.yaml"},
            "baseline": {"refs": [SHA], "reason": "scoped break"},
        }],
    },
}

ABSENT = object()
# Falsy values are the ones an `x or {}` read silently treats as absent.
WRONG_FOR_MAPPING = [None, [], "", 0, False, "bad"]
WRONG_FOR_LIST = [None, {}, "", 0, False, "bad"]


def at(path, value):
    """A copy of VALID with the field at ``path`` set to ``value`` (or removed for ABSENT)."""
    document = copy.deepcopy(VALID)
    *parents, key = path
    target = document
    for part in parents:
        target = target[part]
    if value is ABSENT:
        del target[key]
    else:
        target[key] = value
    return document


CONTRACT = ("spec", "contracts", 0)
CASES = [
    # (label, path, value, valid)
    ("valid", ("metadata",), {"name": "example"}, True),
    *[(f"spec={v!r}", ("spec",), v, False) for v in WRONG_FOR_MAPPING],
    ("spec omitted", ("spec",), ABSENT, False),
    ("contracts empty", ("spec", "contracts"), [], True),
    ("contracts omitted", ("spec", "contracts"), ABSENT, False),
    *[(f"contracts={v!r}", ("spec", "contracts"), v, False) for v in WRONG_FOR_LIST],
    *[(f"contracts[0]={v!r}", CONTRACT, v, False) for v in WRONG_FOR_MAPPING],
    ("exclusions omitted", ("spec", "exclusions"), ABSENT, True),
    ("exclusions empty", ("spec", "exclusions"), [], True),
    *[(f"exclusions={v!r}", ("spec", "exclusions"), v, False) for v in WRONG_FOR_LIST],
    ("candidate omitted", ("spec", "candidate"), ABSENT, True),
    ("candidate empty", ("spec", "candidate"), {}, True),
    *[(f"candidate={v!r}", ("spec", "candidate"), v, False) for v in WRONG_FOR_MAPPING],
    *[(f"candidate.generator={v!r}", ("spec", "candidate", "generator"), v, False) for v in WRONG_FOR_MAPPING],
    ("spec.baseline omitted", ("spec", "baseline"), ABSENT, True),
    *[(f"spec.baseline={v!r}", ("spec", "baseline"), v, False) for v in WRONG_FOR_MAPPING],
    ("policy omitted", (*CONTRACT, "policy"), ABSENT, False),
    *[(f"policy={v!r}", (*CONTRACT, "policy"), v, False) for v in WRONG_FOR_MAPPING],
    ("yqVersion omitted", (*CONTRACT, "policy", "yqVersion"), ABSENT, False),
    ("oasdiffVersion omitted", (*CONTRACT, "policy", "oasdiffVersion"), ABSENT, False),
    ("yqVersion float", (*CONTRACT, "policy", "yqVersion"), 4.45, False),
    ("yqVersion null", (*CONTRACT, "policy", "yqVersion"), None, False),
    ("oasdiffVersion int", (*CONTRACT, "policy", "oasdiffVersion"), 1, False),
    ("severityOverrides omitted", (*CONTRACT, "policy", "severityOverrides"), ABSENT, True),
    *[(f"severityOverrides={v!r}", (*CONTRACT, "policy", "severityOverrides"), v, False) for v in WRONG_FOR_MAPPING],
    ("contract generator omitted", (*CONTRACT, "generator"), ABSENT, True),
    *[(f"contract generator={v!r}", (*CONTRACT, "generator"), v, False) for v in WRONG_FOR_MAPPING],
    ("contract baseline omitted", (*CONTRACT, "baseline"), ABSENT, True),
    *[(f"contract baseline={v!r}", (*CONTRACT, "baseline"), v, False) for v in WRONG_FOR_MAPPING],
]


def schema_accepts(document):
    return not list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(document))


def validator_accepts(document):
    try:
        openapi.validate_descriptor(document)
    except openapi.CheckError:
        return False
    return True


def test_schema_is_a_valid_draft_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("label, path, value, valid", CASES, ids=[case[0] for case in CASES])
def test_schema_and_validator_agree(label, path, value, valid):
    document = at(path, value)
    assert schema_accepts(document) is valid, f"schema: {label}"
    assert validator_accepts(document) is valid, f"validate_descriptor: {label}"


def test_selftest_descriptor_is_valid():
    document = yaml.safe_load((ROOT / "selftest" / "contracts.yaml").read_text(encoding="utf-8"))
    assert schema_accepts(document) and validator_accepts(document)
