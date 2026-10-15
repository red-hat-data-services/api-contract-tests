#!/usr/bin/env python3
"""contract-tests: local runner for shared RHOAI and ODH contract checks.

This repository is the shared-tooling home. The runner currently wraps the
OpenAPI and static CRD compatibility modules; conversion and upgrade testing
remain follow-on work.

The same invocation runs locally and in CI, so results are reproducible
without a cluster:

    python3 cmd/contract-tests/cli.py check \\
      --repo /path/to/provider/checkout \\
      --contract-set .rhoai/contracts.yaml \\
      --baseline-ref <full-git-sha> \\
      --output-dir /tmp/contract-report
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path

_CHECKER_PATH = Path(__file__).resolve().parents[2] / "modules" / "openapi-compat" / "checker.py"
_CRD_CHECKER_PATH = Path(__file__).resolve().parents[2] / "modules" / "crd-compat" / "checker.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("openapi_compat_checker", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_crd_checker():
    spec = importlib.util.spec_from_file_location("crd_compat_checker", _CRD_CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(prog="contract-tests", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Run a registered contract's compatibility check")
    check.add_argument("--repo", type=Path, required=True, help="Path to the provider repository to check")
    check.add_argument("--contract-set", type=Path, required=True,
                        help="Path to the ContractSet descriptor, relative to --repo (e.g. .rhoai/contracts.yaml)")
    check.add_argument("--contract-id", help="Contract id to run; required if the descriptor registers more than one")
    check.add_argument("--baseline-ref", action="append", required=True,
                       help="Full resolved Git SHA; repeat for every supported baseline. Mutable refs are rejected.")
    check.add_argument("--output-dir", type=Path, required=True,
                        help="New report directory; existing paths are never overwritten")
    check.add_argument("--enforce", action="store_true", help="Exit 1 for breaking changes; default is report-only")
    check.add_argument("--oasdiff", default=os.environ.get("OASDIFF"), help="Pinned oasdiff executable")
    check.add_argument("--yq", default=os.environ.get("YQ"), help="Repository's pinned yq executable")

    crd_check = subparsers.add_parser("crd-check", help="Compare CustomResourceDefinitions with immutable Git baselines")
    crd_check.add_argument("--repo", type=Path, required=True, help="Path to the provider repository to check")
    crd_check.add_argument("--crd-glob", required=True, help="Repository-relative glob selecting CustomResourceDefinition YAML files")
    crd_check.add_argument("--baseline-ref", action="append", required=True,
                           help="Full resolved Git SHA; repeat for every supported baseline. Mutable refs are rejected.")
    crd_check.add_argument("--output-dir", type=Path, required=True,
                           help="New report directory; existing paths are never overwritten")
    crd_check.add_argument("--enforce", action="store_true", help="Exit 1 for breaking changes; default is report-only")

    args = parser.parse_args(argv)
    if args.command == "check":
        checker = _load_checker()
        return checker.run_check(args)
    if args.command == "crd-check":
        checker = _load_crd_checker()
        return checker.run_check(args)
    parser.error(f"Unknown command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
