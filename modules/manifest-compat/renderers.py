"""Isolated revision snapshots and reproducible offline manifest rendering.

Implements the standing snapshot/prepare/render contract for the manifest
compatibility check:

- ``snapshot_candidate`` copies the current tracked plus nonignored untracked
  working-tree state (including local edits and deletions) into a private
  regular-file snapshot; ``snapshot_baseline`` materializes the exact Git blobs
  of a resolved commit. Neither mutates the provider checkout, its refs, or any
  submodule/LFS pointer content; inputs that cannot be safely materialized
  (symlinks, gitlinks, LFS pointers, non-regular files) are recorded as hazard
  records and fail closed once selected.
- ``prepare_tools`` is the only network-enabled step: it fetches and verifies
  the pinned Helm/Kustomize modules through Go's normal checksum mechanism and
  primes their compilation. It renders no provider content.
- ``render`` produces rendered bytes for one revision using fixed
  ``go run <module>@<version>`` commands outside any provider-controlled
  go.mod/go.work influence, strictly offline (a ``file://`` module proxy
  rooted at the prepared cache), in clean renderer homes with allowlisted
  subprocess environments and isolated working directories, twice per
  revision to prove determinism. Kustomization references — including
  those nested inside generator/transformer/validator plugin
  configurations — are resolved against the repository snapshot and
  rejected (remote, escaping, hazardous, or missing) before any
  subprocess runs.

All diagnostics are sanitized: rendered manifests, values, and Secret contents
never appear in error messages, and renderer subprocess output is bounded,
truncated, and redacted.
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

# --- Pinned renderer commands (constants; never descriptor-supplied) ---------

HELM_PACKAGE = "helm.sh/helm/v4/cmd/helm"
HELM_MODULE = "helm.sh/helm/v4"
HELM_VERSION = "4.3.1"
HELM_PIN = f"{HELM_PACKAGE}@v{HELM_VERSION}"
KUSTOMIZE_MODULE = "sigs.k8s.io/kustomize/kustomize/v5"
KUSTOMIZE_VERSION = "5.8.3"
KUSTOMIZE_PIN = f"{KUSTOMIZE_MODULE}@v{KUSTOMIZE_VERSION}"

# Minimum local Go toolchain per renderer, from the pinned modules' go.mod.
HELM_GO_MINIMUM = (1, 26, 0)
KUSTOMIZE_GO_MINIMUM = (1, 27, 0)
GO_MINIMUMS: dict[str, tuple[int, int, int]] = {
    "helm": HELM_GO_MINIMUM,
    "kustomize": KUSTOMIZE_GO_MINIMUM,
}

PREPARED_TOOLS: dict[str, dict[str, str]] = {
    "helm": {
        "package": HELM_PACKAGE,
        "module": HELM_MODULE,
        "version": HELM_VERSION,
        "pin": HELM_PIN,
    },
    "kustomize": {
        "package": KUSTOMIZE_MODULE,
        "module": KUSTOMIZE_MODULE,
        "version": KUSTOMIZE_VERSION,
        "pin": KUSTOMIZE_PIN,
    },
}

# --- Bounded execution limits -------------------------------------------------

RENDER_TIMEOUT_SECONDS = 120
PREPARE_TIMEOUT_SECONDS = 900
GIT_TIMEOUT_SECONDS = 120
GO_VERSION_TIMEOUT_SECONDS = 60
MAX_RENDER_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RENDER_DOCUMENTS = 10_000
MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_REDACTION_STRINGS = 4096
MAX_CHART_DEPTH = 8

# --- Snapshot rules -----------------------------------------------------------

_SNAPSHOT_EXCLUDED_TOPLEVEL = (".git", ".context", ".contract-tests")
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# --- Template subset rules ----------------------------------------------------

FORBIDDEN_TEMPLATE_FUNCTIONS = ("lookup", "tpl")
_TEMPLATE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# --- Helm release-name rules --------------------------------------------------

# Mirrors helm.sh/helm/v4@v4.3.1 pkg/chart/v2/util/validate_name.go
# (validName / ValidateReleaseName): a full-match lowercase DNS-subdomain-style
# name of at most 53 characters. A release name is descriptor-controlled and
# becomes a positional argument, so it must never be able to parse as a Helm
# option (--set-file, --values, --output-dir, repository/version flags, ...).
HELM_RELEASE_NAME_MAX_LENGTH = 53
_HELM_RELEASE_NAME_RE = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)

# --- Kustomize rules ----------------------------------------------------------

_KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_REMOTE_REFERENCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_GLOB_CHARS = re.compile(r"[*?\[]")

# Plugin configurations in ``generators``/``transformers``/``validators``
# entries. Only builtin plugin configurations are supported: the pinned
# kustomize configures every document through the *invoking kustomization's*
# loader (internal/target/kusttarget_configplugin.go), so a non-builtin
# configuration is an external exec/go/container plugin, which the pinned
# binary refuses anyway — rejecting it here fails earlier and actionably.
_BUILTIN_PLUGIN_API_VERSION = "builtin"
# Kinds whose configurations hold file references resolved through the
# invoking kustomization's loader (verified against the pinned kustomize
# internal/builtins: every h.Loader().Load call site). Field shapes:
# "file" (one path string), "files" (list of path strings), "replacements"
# (list of mappings with an optional "path"), "kv" (generator sources).
_PLUGIN_REFERENCE_FIELDS: dict[str, dict[str, str]] = {
    "ConfigMapGenerator": {"files": "kv", "envs": "kv", "env": "kv"},
    "SecretGenerator": {"files": "kv", "envs": "kv", "env": "kv"},
    "PatchTransformer": {"path": "file"},
    "PatchJson6902Transformer": {"path": "file"},
    "PatchStrategicMergeTransformer": {"paths": "files"},
    "ReplacementTransformer": {"replacements": "replacements"},
    "ValueAddTransformer": {"targetFilePath": "file"},
}
# The remaining builtin kinds of the pinned kustomize carry no file
# references (internal/plugins/builtinhelpers plus the loader call sites).
_REFERENCE_FREE_PLUGIN_KINDS = frozenset({
    "AnnotationsTransformer", "HashTransformer", "IAMPolicyGenerator",
    "ImageTagTransformer", "LabelTransformer", "NamespaceTransformer",
    "PrefixSuffixTransformer", "PrefixTransformer", "SuffixTransformer",
    "ReplicaCountTransformer",
})

# The pinned kustomize decodes YAML through sigs.k8s.io/yaml, whose JSON step
# matches Go struct field names case-insensitively (stdlib decoder semantics;
# the pinned yaml@v1.6.0 documents this). A key spelled ``Path`` in a plugin
# configuration or ``Resources`` in a kustomization therefore populates the
# real field while this module's case-sensitive readers never see it —
# silently bypassing every reference check below. Keys are validated by
# case-fold collision: a key matching a known kustomize field name only after
# lowercasing is rejected with its canonical spelling. Keys that match no
# field in any casing are ignored by the pinned kustomize (non-strict decode)
# and stay legal. Field names below were extracted from the pinned source:
# sigs.k8s.io/kustomize/api@v0.21.3 types/kustomization.go (Kustomization
# struct), types/{patch,replacement,replacementfield,labels,generatorargs,
# kvpairsources,generatoroptions}.go, and internal/builtins/*.go.
_KUSTOMIZATION_FIELDS = frozenset({
    "kind", "apiVersion", "metadata", "openapi", "namePrefix", "nameSuffix",
    "namespace", "commonLabels", "labels", "commonAnnotations",
    "patchesStrategicMerge", "patchesJson6902", "patches", "images",
    "imageTags", "replacements", "replicas", "vars", "sortOptions",
    "resources", "components", "crds", "bases", "configMapGenerator",
    "secretGenerator", "helmGlobals", "helmCharts",
    "helmChartInflationGenerator", "generatorOptions", "configurations",
    "generators", "transformers", "validators", "buildMetadata",
})
# Top-level fields of every builtin plugin configuration kind, plus the
# generic apiVersion/kind/metadata envelope.
_PLUGIN_SPEC_FIELDS = frozenset({
    "apiVersion", "kind", "metadata", "path", "patch", "target", "options",
    "jsonOp", "paths", "patches", "replacements", "value", "targets",
    "targetFilePath", "name", "namespace", "behavior", "literals", "files",
    "envs", "env", "labels", "fieldSpecs", "prefix", "suffix", "unsetOnly",
    "setRoleBindingSubjects", "annotations", "imageTag", "replica",
    "sortOptions",
})
# Struct-decoded list entries inside a kustomization: the field names of the
# element structs (Patch, ReplacementField, Label, and the generator args).
_KUSTOMIZATION_ENTRY_FIELDS: dict[str, frozenset[str]] = {
    "patches": frozenset({"path", "patch", "target", "options"}),
    "patchesJson6902": frozenset({"target", "path", "jsonOp"}),
    "replacements": frozenset({"source", "targets", "sourceValue", "path"}),
    "labels": frozenset({
        "pairs", "includeSelectors", "includeTemplates",
        "includeVolumeClaimTemplates", "fields",
    }),
    "configMapGenerator": frozenset({
        "name", "namespace", "behavior", "literals", "files", "envs", "env",
        "options",
    }),
    "secretGenerator": frozenset({
        "name", "namespace", "behavior", "literals", "files", "envs", "env",
        "options", "type", "format",
    }),
}


_KELVIN_SIGN = chr(0x212A)     # U+212A KELVIN SIGN
_SMALL_LONG_ESS = chr(0x017F)  # U+017F LATIN SMALL LETTER LONG S


def _go_json_fold_matches(key: str, field: str) -> bool:
    """Whether the pinned kustomize decodes ``key`` into struct field ``field``.

    Replicates the fold selection of the encoding/json fork vendored inside
    sigs.k8s.io/yaml@v1.6.0 (fields.go ``foldFunc``), which is what decodes
    kustomization and plugin documents into Go structs. Field names are the
    ASCII JSON tags (every allowlist entry is pure ASCII letters), so:

    * an ASCII ``key`` matches only case-insensitively (all three ASCII fold
      functions agree for all-letter field names), while
    * a ``key`` containing U+017F (long s) or U+212A (Kelvin sign)
      additionally matches a field holding 's'/'S' or 'k'/'K' at that
      position — but only when the field name contains one of those letters
      at all, because that is what selects ``equalFoldRight``; fields without
      k/K/s/S fold ASCII-only and can never be populated by a non-ASCII key.

    Python's ``str.lower`` does not implement this (the long s lowercases
    to itself), which is why the check cannot simply lowercase both sides.
    """
    if key == field:
        return True
    if key.isascii():
        return key.lower() == field.lower()
    if not any(char in "kKsS" for char in field):
        return False
    if len(key) != len(field):
        return False
    for key_char, field_char in zip(key, field):
        if key_char == _SMALL_LONG_ESS:
            if field_char not in "sS":
                return False
        elif key_char == _KELVIN_SIGN:
            if field_char not in "kK":
                return False
        elif key_char.isascii():
            if ("a" <= field_char <= "z") or ("A" <= field_char <= "Z"):
                if key_char.lower() != field_char.lower():
                    return False
            elif key_char != field_char:
                return False
        else:
            return False
    return True


def _reject_case_folded_keys(mapping: Mapping[str, Any],
                             field_names: frozenset[str], where: str) -> None:
    """Reject keys that match kustomize struct fields only by folding.

    Complements the case-sensitive readers: any key the pinned kustomize
    would decode into a struct field must carry that field's exact canonical
    spelling, or the reference validation reading the lowercase name would
    never see it. Folding follows the Go decoder exactly, including the
    Unicode equivalents of 's' and 'k' (U+017F, U+212A) that a plain
    lowercasing comparison misses.
    """
    for key in mapping:
        if not isinstance(key, str):
            continue
        for canonical in field_names:
            if _go_json_fold_matches(key, canonical) and key != canonical:
                raise RenderError(
                    f"{where}: field {key!r} must be spelled {canonical!r}; the pinned "
                    "kustomize matches YAML keys case-insensitively, so a differently "
                    "cased spelling would bypass input validation"
                )

_GO_VERSION_RE = re.compile(r"go(\d+)\.(\d+)(?:\.(\d+))?")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=]{40,}")


class RenderError(Exception):
    """A snapshot, preparation, or rendering step could not complete safely."""


class MissingInputError(RenderError):
    """Selected scenario inputs are absent from the revision snapshot.

    Distinct from a successful render that produces zero objects: a missing
    input can never compare as an empty object map. Role-specific decisions
    (historical-empty rejection, explicit-new baseline declarations) belong to
    the checker; this error carries the exact missing and matched inputs so the
    checker can distinguish a fully absent source set from a partial one.
    """

    def __init__(self, message: str, *, missing: Sequence[str], matched: Sequence[str]) -> None:
        super().__init__(message)
        self.missing: list[str] = list(missing)
        self.matched: list[str] = list(matched)


# --- Subprocess execution -----------------------------------------------------


def _run(argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
         timeout: float, stdout_limit: int,
         stderr_limit: int = MAX_DIAGNOSTIC_BYTES) -> subprocess.CompletedProcess[bytes]:
    """Run a command with bounded capture: capped output, hard timeout.

    stdout is truncated at ``stdout_limit`` bytes (the caller reports the
    overflow), stderr at ``stderr_limit``. Raises ``subprocess.TimeoutExpired``
    on timeout and ``OSError`` when the command cannot start.
    """
    command = [str(argument) for argument in argv]
    process = subprocess.Popen(
        command,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + timeout
    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    remaining_streams = 2
    timed_out = False
    try:
        while remaining_streams:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            events = selector.select(timeout=left)
            if not events:
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    remaining_streams -= 1
                    continue
                if key.fileobj is process.stdout:
                    if len(stdout) < stdout_limit:
                        stdout.extend(chunk[: stdout_limit - len(stdout)])
                elif len(stderr) < stderr_limit:
                    stderr.extend(chunk[: stderr_limit - len(stderr)])
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout)
        returncode = process.wait(timeout=30)
        return subprocess.CompletedProcess(command, returncode, bytes(stdout), bytes(stderr))
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _git(repo: Path, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run a read-only git command in ``repo`` with replace objects disabled."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = _run(
            ["git", "--no-replace-objects", *args],
            cwd=repo, env=env, timeout=GIT_TIMEOUT_SECONDS, stdout_limit=MAX_GIT_OUTPUT_BYTES,
        )
    except subprocess.TimeoutExpired as error:
        raise RenderError(f"git {args[0]} timed out in {repo}") from error
    except OSError as error:
        raise RenderError(f"cannot run git in {repo}: {error}") from error
    if check and result.returncode != 0:
        raise RenderError(f"git {args[0]} failed in {repo} (exit code {result.returncode})")
    return result


def _new_renderer_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="manifest-render-home-"))


def _remove_renderer_home(home: Path) -> None:
    shutil.rmtree(home, ignore_errors=True)


# Explicit child-environment allowlist. Renderer subprocesses inherit only
# the process search path (locating the pinned go toolchain); online
# preparation additionally inherits the system CA bundle locations, because
# fetching modules over TLS must trust the host's store. Everything else —
# ambient credentials, kubeconfig, proxy settings, Helm/Kustomize/Go
# overrides — is dropped rather than filtered, so auditing what a renderer
# subprocess can see is a read of this tuple. This is process-level hygiene
# only: OS-enforced isolation (network namespaces, sandboxes) is out of
# scope.
_TOOL_ENV_ALLOWLIST = ("PATH",)
_PREPARATION_ENV_ALLOWLIST = _TOOL_ENV_ALLOWLIST + ("SSL_CERT_FILE", "SSL_CERT_DIR")


def _tool_env(cache_dir: Path | None, home: Path, *, offline: bool) -> dict[str, str]:
    """Environment for go commands, isolated from provider and user influence.

    Only the allowlisted variables are inherited from the caller (see
    ``_TOOL_ENV_ALLOWLIST``); ambient credentials, proxies, and tool
    overrides never reach a renderer subprocess. go.work/go.mod influence
    is disabled (``GOWORK=off``, ``GOENV=off``), the toolchain is pinned to
    the local install (``GOTOOLCHAIN=local``), and HOME, XDG directories,
    and the module/build caches point into isolated locations. Render
    commands additionally run strictly offline (``GOSUMDB=off`` and a
    ``file://`` module proxy rooted at the prepared cache's download
    directory, which can only serve what preparation fetched and verified);
    preparation instead forces Go's checksum database and resolves modules
    through Go's own defaults, not an ambient proxy configuration.
    """
    env = {
        key: os.environ[key]
        for key in (_PREPARATION_ENV_ALLOWLIST if not offline else _TOOL_ENV_ALLOWLIST)
        if key in os.environ
    }
    env.update({
        "GOWORK": "off",
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "",
        "CGO_ENABLED": "0",
        "HOME": str(home),
        "TMPDIR": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    })
    if cache_dir is not None:
        env["GOMODCACHE"] = str(cache_dir / "gomodcache")
        env["GOCACHE"] = str(cache_dir / "gocache")
        env["GOPATH"] = str(cache_dir / "gopath")
    if offline:
        # A plain GOPROXY=off cannot run "go run <module>@<version>": go's
        # resolution of the version-qualified argument queries the module
        # proxy for the latest version (deprecation and retraction checks)
        # and fails even when the exact module is already cached. Pointing
        # the proxy at the cache's own download directory keeps rendering
        # strictly offline — a file:// proxy serves only local files — while
        # a missing or incomplete cache still fails closed. The proxy-bypass
        # variables are cleared exactly as in the online branch: an ambient
        # GOPRIVATE/GONOPROXY would otherwise send matching modules to
        # direct VCS resolution, which is a network fetch.
        if cache_dir is None:
            env["GOPROXY"] = "off"
        else:
            download = cache_dir / "gomodcache" / "cache" / "download"
            env["GOPROXY"] = Path(os.path.abspath(str(download))).as_uri()
        env["GOSUMDB"] = "off"
        env["GOPRIVATE"] = ""
        env["GONOSUMDB"] = ""
        env["GONOPROXY"] = ""
    else:
        # Force verification through Go's normal checksum mechanism; an
        # ambient GOPRIVATE must never silently disable it.
        env["GOSUMDB"] = "sum.golang.org"
        env["GOPRIVATE"] = ""
        env["GONOSUMDB"] = ""
        env["GONOPROXY"] = ""
    return env


# --- Revision snapshots -------------------------------------------------------


def _is_excluded(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts:
        return True
    if path.parts[0] in _SNAPSHOT_EXCLUDED_TOPLEVEL:
        return True
    return any(part == ".git" for part in path.parts)


def _hazard_record(destination: Path, name: str, hazard: str) -> dict[str, Any]:
    return {"file": name, "root": str(destination), "hazard": hazard}


def _ancestor_is_symlink(repo: Path, name: str, cache: dict[str, bool]) -> bool:
    """Whether any directory between ``repo`` and ``name`` is a symlink.

    ``lstat`` and ``O_NOFOLLOW`` on the file itself protect only the final
    path component; a tracked directory replaced locally by a symlink would
    otherwise let its indexed descendant files be copied from outside the
    checkout. Results are cached per parent directory.
    """
    parent = PurePosixPath(name).parent.as_posix()
    if parent in cache:
        return cache[parent]
    result = False
    current = repo
    for part in PurePosixPath(name).parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as error:
            raise RenderError(
                f"cannot inspect repository directory {current!r}: {error}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            result = True
            break
    cache[parent] = result
    return result


def _copy_candidate_entry(repo: Path, name: str, mode: str | None,
                          destination: Path,
                          dir_cache: dict[str, bool]) -> dict[str, Any] | None:
    """Copy one working-tree entry, never following symlinks.

    Returns a hazard record (no materialized copy) for entries that cannot be
    safely materialized, ``None`` for tracked files deleted locally, and a
    regular input record otherwise.
    """
    if mode == "160000":
        return _hazard_record(destination, name, "gitlink")
    source = repo / name
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RenderError(f"cannot inspect repository file {name!r}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        return _hazard_record(destination, name, "symlink")
    if not stat.S_ISREG(info.st_mode):
        return _hazard_record(destination, name, "non-regular")
    if _ancestor_is_symlink(repo, name, dir_cache):
        return _hazard_record(destination, name, "symlink-ancestor")
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            return _hazard_record(destination, name, "symlink")
        raise RenderError(f"cannot open repository file {name!r}: {error}") from error
    digest = hashlib.sha256()
    target_path = destination / name
    try:
        with os.fdopen(source_fd, "rb") as source_file:
            first = source_file.read(1024 * 1024)
            if first.startswith(_LFS_POINTER_PREFIX):
                return _hazard_record(destination, name, "lfs-pointer")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(target_fd, "wb") as target_file:
                digest.update(first)
                target_file.write(first)
                while True:
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target_file.write(chunk)
    except OSError as error:
        raise RenderError(f"cannot copy repository file {name!r}: {error}") from error
    os.chmod(target_path, 0o600)
    return {
        "file": name,
        "root": str(destination),
        "path": str(target_path),
        "sha256": digest.hexdigest(),
    }


def snapshot_candidate(repo: Path, destination: Path) -> list[dict[str, Any]]:
    """Snapshot the current working-tree state of ``repo`` into ``destination``.

    Reflects tracked files (with local edits), nonignored untracked files, and
    local deletions; excludes ``.git``, ``.context``, the ``.contract-tests``
    tooling checkout, and anything below a ``.git`` directory. The snapshot is
    a fresh private directory of regular-file copies with SHA-256 digests,
    never a worktree or checkout. Symlinks, gitlinks, LFS pointers, and other
    non-regular entries are recorded as hazards without being materialized.
    """
    repo = Path(repo)
    destination = Path(destination)
    if not repo.is_dir():
        raise RenderError(f"repository {repo} is not a directory")
    listing = _git(repo, ["ls-files", "-z", "-s", "--cached", "--others", "--exclude-standard"])
    try:
        entries = sorted({chunk.decode("utf-8") for chunk in listing.stdout.split(b"\0") if chunk})
    except UnicodeDecodeError as error:
        raise RenderError("repository contains file names that are not valid UTF-8") from error

    created = False
    try:
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise RenderError(
                f"snapshot destination {destination} cannot be created: {error}"
            ) from error
        created = True
        os.chmod(destination, 0o700)
        records: list[dict[str, Any]] = []
        dir_cache: dict[str, bool] = {}
        for entry in entries:
            if "\t" in entry:
                meta, name = entry.split("\t", 1)
                mode = meta.split(" ", 1)[0]
            else:
                name, mode = entry, None
            if _is_excluded(name):
                continue
            record = _copy_candidate_entry(repo, name, mode, destination, dir_cache)
            if record is not None:
                records.append(record)
        records.sort(key=lambda record: record["file"])
        return records
    except BaseException:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise


def _resolve_commit(repo: Path, ref: str) -> str:
    result = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        check=False,
    )
    resolved = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode or _SHA_PATTERN.fullmatch(resolved) is None:
        raise RenderError(
            f"cannot resolve revision {ref!r} as a commit; fetch the trusted revision first"
        )
    if len(resolved) == len(ref) and resolved != ref:
        raise RenderError(
            f"revision {ref!r} resolves to a different commit ({resolved}); "
            "a full object ID must resolve to itself"
        )
    return resolved


def snapshot_baseline(repo: Path, sha: str, destination: Path) -> list[dict[str, Any]]:
    """Snapshot the exact Git blobs of commit ``sha`` into ``destination``.

    ``sha`` must be a full 40- or 64-character lowercase hex object ID and is
    resolved as a commit before anything is read. Blobs are read with
    ``--no-replace-objects`` via ``cat-file`` (never a checkout, archive, or
    smudged export), so the materialized bytes are exactly the stored objects.
    Symlinks, gitlinks, and LFS pointers become hazard records.
    """
    repo = Path(repo)
    destination = Path(destination)
    if not repo.is_dir():
        raise RenderError(f"repository {repo} is not a directory")
    if not isinstance(sha, str) or _SHA_PATTERN.fullmatch(sha) is None:
        raise RenderError(
            f"baseline revision {sha!r} must be a full 40- or 64-character lowercase Git SHA; "
            "mutable refs such as main, HEAD, tags, and branches are not accepted"
        )
    resolved = _resolve_commit(repo, sha)
    listing = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", resolved])
    entries: list[tuple[str, str, str, str]] = []
    for chunk in listing.stdout.split(b"\0"):
        if not chunk:
            continue
        try:
            meta, raw_name = chunk.split(b"\t", 1)
            mode, otype, oid = meta.decode("ascii").split(" ")
            name = raw_name.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise RenderError("cannot read the baseline tree listing") from error
        entries.append((mode, otype, oid, name))
    entries.sort(key=lambda entry: entry[3])

    created = False
    try:
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise RenderError(
                f"snapshot destination {destination} cannot be created: {error}"
            ) from error
        created = True
        os.chmod(destination, 0o700)
        records: list[dict[str, Any]] = []
        for mode, otype, oid, name in entries:
            if _is_excluded(name):
                continue
            if otype == "commit" or mode == "160000":
                records.append(_hazard_record(destination, name, "gitlink"))
                continue
            if otype != "blob":
                raise RenderError(
                    f"baseline tree entry {name!r} has unsupported object type {otype!r}"
                )
            if mode == "120000":
                records.append(_hazard_record(destination, name, "symlink"))
                continue
            content = _git(repo, ["cat-file", "blob", oid]).stdout
            if content.startswith(_LFS_POINTER_PREFIX):
                records.append(_hazard_record(destination, name, "lfs-pointer"))
                continue
            target_path = destination / name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                target_fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(target_fd, "wb") as target_file:
                    target_file.write(content)
            except OSError as error:
                raise RenderError(f"cannot write snapshot file {name!r}: {error}") from error
            os.chmod(target_path, 0o600)
            records.append({
                "file": name,
                "root": str(destination),
                "path": str(target_path),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        return records
    except BaseException:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise


# --- Snapshot record access ---------------------------------------------------


class _Snapshot:
    """Validated view over the records returned by the snapshot functions."""

    def __init__(self, snapshot: Sequence[Mapping[str, Any]]) -> None:
        self.files: dict[str, Mapping[str, Any]] = {}
        self.hazards: dict[str, Mapping[str, Any]] = {}
        roots: set[str] = set()
        for index, record in enumerate(snapshot):
            if not isinstance(record, Mapping):
                raise RenderError(f"snapshot record {index} is not a mapping")
            file = record.get("file")
            root = record.get("root")
            if not isinstance(file, str) or not file or not isinstance(root, str) or not root:
                raise RenderError(f"snapshot record {index} is missing 'file' or 'root'")
            roots.add(root)
            if "hazard" in record:
                self.hazards[file] = record
            else:
                path = record.get("path")
                if not isinstance(path, str) or not path:
                    raise RenderError(f"snapshot record {index} is missing 'path'")
                self.files[file] = record
        if len(roots) > 1:
            raise RenderError("snapshot records span multiple roots")
        self.root: str | None = (roots.pop() if roots else None)
        directories: set[str] = set()
        for file in self.files:
            parent = PurePosixPath(file).parent
            while parent != PurePosixPath("."):
                directories.add(parent.as_posix())
                parent = parent.parent
        self.directories = directories

    def read(self, file: str) -> bytes:
        record = self.files.get(file)
        if record is None:
            raise RenderError(f"snapshot file {file!r} is not available")
        try:
            content = Path(str(record["path"])).read_bytes()
        except OSError as error:
            raise RenderError(f"cannot read snapshot file {file!r}: {error}") from error
        if len(content) > MAX_INPUT_FILE_BYTES:
            raise RenderError(f"snapshot file {file!r} exceeds the input size limit")
        if hashlib.sha256(content).hexdigest() != record.get("sha256"):
            raise RenderError(f"snapshot file {file!r} changed after the snapshot was taken")
        return content


# --- Canonical document handling ----------------------------------------------


class _CanonicalLoader(yaml.SafeLoader):
    """SafeLoader that keeps date-like scalars as strings.

    Canonicalization only proves determinism; it must not fail on scalars a
    renderer may legitimately emit unquoted.
    """


_CanonicalLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _canonical_documents(content: bytes, source: str) -> list[str]:
    """Parse rendered bytes into a sorted list of canonical document strings.

    Map key order and document order are ignored (they are harmless), while
    any content difference is preserved. Empty separators yield no documents.
    """
    try:
        documents = list(yaml.load_all(content, Loader=_CanonicalLoader))
    except yaml.YAMLError as error:
        raise RenderError(
            f"{source}: rendered output is not valid YAML ({type(error).__name__})"
        ) from error
    canonical = [
        json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
        for document in documents
        if document is not None
    ]
    return sorted(canonical)


def _parse_yaml_document(content: bytes, source: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise RenderError(f"{source}: cannot parse YAML ({type(error).__name__})") from error
    if not isinstance(document, dict):
        raise RenderError(f"{source}: expected a YAML mapping")
    return document


# --- Helm template subset scanning --------------------------------------------


def _skip_string(body: str, start: int, quote: str, source: str, *, escapes: bool) -> int:
    position = start + 1
    while position < len(body):
        character = body[position]
        if escapes and character == "\\":
            position += 2
            continue
        if character == quote:
            return position + 1
        position += 1
    raise RenderError(f"{source}: unparseable template action (unterminated string)")


def _skip_field_chain(body: str, start: int) -> int:
    position = start + 1
    match = _TEMPLATE_IDENT.match(body, position)
    if match is not None:
        position = match.end()
    while position < len(body) and body[position] == ".":
        match = _TEMPLATE_IDENT.match(body, position + 1)
        position = match.end() if match is not None else position + 1
    return position


def _scan_action(body: str, source: str) -> None:
    stripped = body.strip()
    if stripped.startswith("-"):
        stripped = stripped[1:].strip()
    if stripped.endswith("-"):
        stripped = stripped[:-1].strip()
    if stripped.startswith("/*"):
        if not stripped.endswith("*/"):
            raise RenderError(f"{source}: unparseable template action (unterminated comment)")
        return
    position = 0
    while position < len(body):
        character = body[position]
        if character.isspace():
            position += 1
        elif character in ('"', "'"):
            position = _skip_string(body, position, character, source, escapes=True)
        elif character == "`":
            position = _skip_string(body, position, character, source, escapes=False)
        elif character == ".":
            position = _skip_field_chain(body, position)
        elif character == "$":
            position += 1
            match = _TEMPLATE_IDENT.match(body, position)
            if match is not None:
                position = match.end()
        else:
            match = _TEMPLATE_IDENT.match(body, position)
            if match is None:
                position += 1
            else:
                identifier = match.group(0)
                if identifier in FORBIDDEN_TEMPLATE_FUNCTIONS:
                    raise RenderError(
                        f"{source}: chart templates may not use the {identifier!r} function; "
                        "charts requiring it are unsupported"
                    )
                position = match.end()


def _scan_template_text(content: bytes, source: str) -> None:
    """Reject bare lookup/tpl function identifiers inside template actions.

    Tokenization distinguishes quoted strings, raw strings, comments, field
    selectors, and variables from bare function identifiers; anything
    unparseable fails closed. Plain text outside actions is data.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RenderError(f"{source}: template file is not valid UTF-8") from error
    position = 0
    while True:
        start = text.find("{{", position)
        if start < 0:
            return
        end = text.find("}}", start + 2)
        if end < 0:
            raise RenderError(f"{source}: unparseable template action (unclosed action)")
        _scan_action(text[start + 2:end], source)
        position = end + 2


# --- Helm chart trees ---------------------------------------------------------


class _ChartTree(Protocol):
    """A chart directory layout: relative names plus bounded reads."""

    names: list[str]

    def read(self, name: str) -> bytes: ...

    def subtree(self, relative_dir: str) -> _ChartTree: ...


class _RecordTree:
    """Chart tree backed by snapshot records under a repo-relative prefix."""

    def __init__(self, snapshot: _Snapshot, prefix: str) -> None:
        self._snapshot = snapshot
        self.prefix = prefix
        self.names = sorted(
            file[len(prefix):] for file in snapshot.files if file.startswith(prefix)
        )

    def read(self, name: str) -> bytes:
        return self._snapshot.read(f"{self.prefix}{name}")

    def subtree(self, relative_dir: str) -> _RecordTree:
        return _RecordTree(self._snapshot, f"{self.prefix}{relative_dir.strip('/')}/")


class _MemberTree:
    """Chart tree backed by in-memory archive members (never extracted)."""

    def __init__(self, members: Mapping[str, bytes], prefix: str = "") -> None:
        self._members = members
        self.prefix = prefix
        self.names = sorted(
            name[len(prefix):] for name in members if name.startswith(prefix)
        )

    def read(self, name: str) -> bytes:
        return self._members[f"{self.prefix}{name}"]

    def subtree(self, relative_dir: str) -> _MemberTree:
        return _MemberTree(self._members, f"{self.prefix}{relative_dir.strip('/')}/")


def _parse_chart_metadata(content: bytes, source: str) -> dict[str, Any]:
    document = _parse_yaml_document(content, source)
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise RenderError(f"{source}: chart metadata must declare a name")
    return document


def _read_chart_archive(data: bytes, source: str) -> dict[str, bytes]:
    """Safely inspect a vendored .tgz chart archive in memory."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise RenderError(f"{source}: chart archive exceeds the input size limit")
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                name = member.name
                path = PurePosixPath(name)
                if not name or path.is_absolute() or any(part == ".." for part in path.parts):
                    raise RenderError(
                        f"{source}: archive member {name!r} is not a safely contained path"
                    )
                if member.isdir():
                    continue
                if member.issym() or member.islnk():
                    raise RenderError(
                        f"{source}: archive member {name!r} is a symlink; unsupported"
                    )
                if not member.isfile():
                    raise RenderError(
                        f"{source}: archive member {name!r} is not a regular file"
                    )
                if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise RenderError(
                        f"{source}: archive member {name!r} exceeds the input size limit"
                    )
                handle = archive.extractfile(member)
                if handle is None:
                    raise RenderError(f"{source}: cannot read archive member {name!r}")
                members[name] = handle.read()
    except tarfile.TarError as error:
        raise RenderError(
            f"{source}: cannot inspect chart archive ({type(error).__name__})"
        ) from error
    return members


def _scan_chart_tree(tree: _ChartTree, label: str, depth: int) -> None:
    """Validate one chart tree: dependencies and the frozen template subset.

    Rejects remote or absent dependencies, scans every template action in the
    chart and all recursive vendored subcharts (directories and archives), and
    fails closed on anything unparseable.
    """
    if depth > MAX_CHART_DEPTH:
        raise RenderError(f"{label}: vendored chart nesting exceeds {MAX_CHART_DEPTH} levels")
    if "Chart.yaml" not in tree.names:
        raise RenderError(f"{label}: chart is missing Chart.yaml")
    metadata = _parse_chart_metadata(tree.read("Chart.yaml"), f"{label}/Chart.yaml")
    dependencies = metadata.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise RenderError(f"{label}/Chart.yaml: dependencies must be a list")

    subchart_dirs = sorted({
        name.split("/")[1]
        for name in tree.names
        if len(name.split("/")) == 3 and name.startswith("charts/") and name.endswith("/Chart.yaml")
    })
    archives = sorted(
        name for name in tree.names
        if len(name.split("/")) == 2 and name.startswith("charts/") and name.endswith(".tgz")
    )

    for name in tree.names:
        if name.startswith("templates/") or "/templates/" in name:
            _scan_template_text(tree.read(name), f"{label}/{name}")

    vendored: dict[str, str] = {}
    for directory in subchart_dirs:
        sub_label = f"{label}/charts/{directory}"
        sub_metadata = _parse_chart_metadata(
            tree.read(f"charts/{directory}/Chart.yaml"), f"{sub_label}/Chart.yaml"
        )
        vendored[sub_metadata["name"]] = f"charts/{directory}"
    archive_trees: list[tuple[str, str, _MemberTree]] = []
    for archive in archives:
        members = _read_chart_archive(tree.read(archive), f"{label}/{archive}")
        roots = sorted({name.split("/", 1)[0] for name in members})
        for root in roots:
            sub_tree = _MemberTree(members, f"{root}/")
            if "Chart.yaml" not in sub_tree.names:
                raise RenderError(f"{label}/{archive}: archive chart {root!r} has no Chart.yaml")
            sub_metadata = _parse_chart_metadata(
                sub_tree.read("Chart.yaml"), f"{label}/{archive}/{root}/Chart.yaml"
            )
            vendored[sub_metadata["name"]] = f"{archive}:{root}"
            archive_trees.append((archive, root, sub_tree))
    for entry in dependencies:
        if not isinstance(entry, dict):
            raise RenderError(f"{label}/Chart.yaml: dependency entries must be mappings")
        dep_name = entry.get("name")
        if not isinstance(dep_name, str) or not dep_name:
            raise RenderError(f"{label}/Chart.yaml: dependency entry without a name")
        repository = entry.get("repository")
        if isinstance(repository, str) and repository:
            raise RenderError(
                f"{label}: dependency {dep_name!r} declares repository {repository!r}; "
                "only dependencies committed under charts/ are supported"
            )
        alias = entry.get("alias")
        candidates = {dep_name}
        if isinstance(alias, str) and alias:
            candidates.add(alias)
        if not candidates & set(vendored):
            raise RenderError(
                f"{label}: dependency {dep_name!r} is declared but not vendored under charts/; "
                "run helm dependency build and commit the result"
            )
    for directory in subchart_dirs:
        _scan_chart_tree(tree.subtree(f"charts/{directory}"), f"{label}/charts/{directory}", depth + 1)
    for archive, root, sub_tree in archive_trees:
        _scan_chart_tree(sub_tree, f"{label}/{archive}/{root}", depth + 1)


# --- Secret redaction ---------------------------------------------------------


def _collect_string_leaves(value: Any, markers: set[str]) -> None:
    if isinstance(value, str):
        if 4 <= len(value) <= 256:
            markers.add(value)
    elif isinstance(value, list):
        for item in value:
            _collect_string_leaves(item, markers)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_string_leaves(item, markers)


def _secret_markers_from_yaml(content: bytes, markers: set[str]) -> None:
    try:
        documents = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return
    for document in documents:
        if not isinstance(document, dict):
            continue
        if document.get("kind") == "Secret":
            for field in ("data", "stringData"):
                values = document.get(field)
                if isinstance(values, dict):
                    _collect_string_leaves(list(values.values()), markers)
        for field in ("configMapGenerator", "secretGenerator"):
            generators = document.get(field)
            if not isinstance(generators, list):
                continue
            for generator in generators:
                if not isinstance(generator, dict):
                    continue
                literals = generator.get("literals")
                if isinstance(literals, list):
                    for literal in literals:
                        if isinstance(literal, str) and "=" in literal:
                            markers.add(literal.split("=", 1)[1])


def _helm_secret_markers(snapshot: _Snapshot, chart_prefix: str,
                         values_rels: Sequence[str],
                         set_values: Mapping[str, str]) -> list[str]:
    """Redaction markers for helm diagnostics.

    Covers the selected values files and set values (as before) plus every
    values.yaml under the chart, including vendored subcharts: chart defaults
    are as likely to be echoed by a failing template as selected values are.
    """
    markers: set[str] = set()
    for rel in values_rels:
        _secret_markers_from_yaml(snapshot.read(rel), markers)
        try:
            _collect_string_leaves(yaml.safe_load(snapshot.read(rel)), markers)
        except yaml.YAMLError:
            pass
    for file in sorted(snapshot.files):
        if not file.startswith(chart_prefix):
            continue
        tail = file[len(chart_prefix):]
        if tail in ("values.yaml", "values.yml") or tail.endswith("/values.yaml") \
                or tail.endswith("/values.yml"):
            _secret_markers_from_yaml(snapshot.read(file), markers)
            try:
                _collect_string_leaves(yaml.safe_load(snapshot.read(file)), markers)
            except yaml.YAMLError:
                pass
        elif tail.endswith(".tgz"):
            # Vendored chart archives are unpacked in memory by helm's
            # dependency handling; their values.yaml members are chart
            # defaults exactly like directory subcharts'.
            members = _read_chart_archive(snapshot.read(file), file)
            for name, content in sorted(members.items()):
                if name in ("values.yaml", "values.yml") or name.endswith("/values.yaml") \
                        or name.endswith("/values.yml"):
                    _secret_markers_from_yaml(content, markers)
                    try:
                        _collect_string_leaves(yaml.safe_load(content), markers)
                    except yaml.YAMLError:
                        pass
    for value in set_values.values():
        if isinstance(value, str) and 4 <= len(value) <= 256:
            markers.add(value)
    return sorted(markers)[:MAX_REDACTION_STRINGS]


def _kustomize_secret_markers(snapshot: _Snapshot, prefix: str) -> list[str]:
    markers: set[str] = set()
    for file in sorted(snapshot.files):
        if not file.startswith(prefix) or not file.endswith((".yaml", ".yml")):
            continue
        _secret_markers_from_yaml(snapshot.read(file), markers)
    return sorted(markers)[:MAX_REDACTION_STRINGS]


def _sanitize_diagnostics(stderr: bytes, secrets: Sequence[str],
                          private_roots: Sequence[str] = ()) -> str:
    """Reduce subprocess stderr to a short, redacted, safe excerpt.

    ``private_roots`` (snapshot and renderer-home locations) are replaced
    with a stable placeholder: they are random per run, and error text
    quoting them would make failing reports non-deterministic and would leak
    local filesystem layout. Replacements run on the full excerpt before
    line truncation so a value straddling a truncation boundary cannot
    partially survive.
    """
    text = stderr[:MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace")
    for root in private_roots:
        if root:
            text = text.replace(root, "<private>")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    lines = [line for line in text.splitlines() if line.strip()][:5]
    summary = " | ".join(line[:200] for line in lines)
    return _LONG_TOKEN_RE.sub("[redacted]", summary) or "(no diagnostics)"


# --- Go toolchain and preparation ---------------------------------------------


def _parse_go_version(output: str) -> tuple[int, int, int]:
    match = _GO_VERSION_RE.search(output)
    if match is None:
        raise RenderError(f"cannot determine the local Go version from {output.strip()!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _check_toolchain(renderers: set[str], env: Mapping[str, str],
                     cwd: Path | None = None) -> str:
    try:
        result = _run(
            ["go", "version"], env=dict(env), cwd=cwd,
            timeout=GO_VERSION_TIMEOUT_SECONDS, stdout_limit=4096,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise RenderError(
            f"cannot run go: {error}; rendering requires a local Go toolchain"
        ) from error
    if result.returncode != 0:
        raise RenderError("go version failed; rendering requires a local Go toolchain")
    output = result.stdout.decode("utf-8", errors="replace").strip()
    version = _parse_go_version(output)
    minimum = max(GO_MINIMUMS[name] for name in renderers)
    if version < minimum:
        raise RenderError(
            f"local Go {version[0]}.{version[1]}.{version[2]} is too old for "
            f"{', '.join(sorted(renderers))} rendering (requires at least "
            f"{minimum[0]}.{minimum[1]}.{minimum[2]}); install Go 1.27 or newer"
        )
    return output.splitlines()[0] if output else ""


def _verify_prepared_cache(cache_dir: Path, renderer: str) -> None:
    tool = PREPARED_TOOLS[renderer]
    module_dir = cache_dir / "gomodcache" / f"{tool['module']}@v{tool['version']}"
    if not module_dir.is_dir():
        raise RenderError(
            f"prepared tool cache at {cache_dir} does not contain {renderer} "
            f"{tool['pin']}; run manifest-prepare with --tool-cache-dir first"
        )


def prepare_tools(config: Mapping[str, Any], cache_dir: Path | None) -> dict[str, Any]:
    """Prepare the pinned renderer modules in ``cache_dir`` (network-enabled).

    The only network-enabled step: fetches and verifies the exact modules
    through Go's normal checksum mechanism, primes their compilation with the
    pinned ``version`` commands, re-runs each one offline through the cache's
    own file proxy to prove the cache is render-ready, and records
    provenance. Renders no provider content. Raw-only selections prepare
    nothing and need no cache.
    """
    scenarios = config.get("scenarios")
    if not isinstance(scenarios, list):
        raise RenderError("configuration has no scenario list")
    selected: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        renderer = scenario.get("renderer")
        if isinstance(renderer, str) and renderer in PREPARED_TOOLS:
            selected.add(renderer)
    needed = sorted(selected)
    if not needed:
        return {
            "needed": [],
            "tools": [],
            "go": None,
            "cache": None,
            "checksum_verification": "not applicable; no renderer scenarios selected",
            "provider_content_rendered": False,
        }
    if cache_dir is None:
        raise RenderError("a tool cache directory is required when renderer scenarios are selected")
    cache = Path(cache_dir)
    tools_spec = config.get("tools") or {}
    if not isinstance(tools_spec, dict):
        raise RenderError("configuration tools must be a mapping")
    for renderer in needed:
        expected = PREPARED_TOOLS[renderer]["version"]
        if tools_spec.get(f"{renderer}Version") != expected:
            raise RenderError(
                f"spec.tools.{renderer}Version must be pinned to {expected!r} "
                "for the selected renderer scenarios"
            )
    try:
        if cache.exists() and not cache.is_dir():
            raise RenderError(f"tool cache location {cache} is not a directory")
        cache.mkdir(parents=True, exist_ok=True)
        for name in ("gomodcache", "gocache", "gopath"):
            (cache / name).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RenderError(f"cannot use tool cache location {cache}: {error}") from error

    home = _new_renderer_home()
    try:
        env = _tool_env(cache, home, offline=False)
        go_version = _check_toolchain(set(needed), env, cwd=home)
        tools: list[dict[str, Any]] = []
        for renderer in needed:
            tool = PREPARED_TOOLS[renderer]
            argv = ["go", "run", tool["pin"], "version"]
            try:
                result = _run(
                    argv, env=env, cwd=home, timeout=PREPARE_TIMEOUT_SECONDS,
                    stdout_limit=MAX_DIAGNOSTIC_BYTES,
                )
            except subprocess.TimeoutExpired as error:
                raise RenderError(
                    f"preparing {renderer} {tool['pin']} exceeded the "
                    f"{PREPARE_TIMEOUT_SECONDS}-second timeout"
                ) from error
            except OSError as error:
                raise RenderError(f"cannot run go: {error}") from error
            if result.returncode != 0:
                raise RenderError(
                    f"cannot prepare {renderer} {tool['pin']} (exit code {result.returncode}): "
                    f"{_sanitize_diagnostics(result.stderr, [])}"
                )
            observed = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
            minimum = GO_MINIMUMS[renderer]
            tools.append({
                "renderer": renderer,
                "module": tool["module"],
                "version": tool["version"],
                "pin": tool["pin"],
                "command": list(argv),
                "go_minimum": f"{minimum[0]}.{minimum[1]}.{minimum[2]}",
                "observed_version": (observed[0][:200] if observed else ""),
                "checksum_verification": "verified by the Go checksum database (GOSUMDB)",
            })
        # Prove the prepared cache can render offline: rerun each pinned
        # version command with the module proxy pointed at the cache itself.
        # A file:// proxy serves only local files, so this fails loudly when
        # anything the go-run resolution needs is missing from the cache.
        for renderer in needed:
            tool = PREPARED_TOOLS[renderer]
            argv = ["go", "run", tool["pin"], "version"]
            try:
                result = _run(
                    argv, env=_tool_env(cache, home, offline=True), cwd=home,
                    timeout=PREPARE_TIMEOUT_SECONDS,
                    stdout_limit=MAX_DIAGNOSTIC_BYTES,
                )
            except subprocess.TimeoutExpired as error:
                raise RenderError(
                    f"verifying offline use of {renderer} {tool['pin']} exceeded the "
                    f"{PREPARE_TIMEOUT_SECONDS}-second timeout"
                ) from error
            except OSError as error:
                raise RenderError(f"cannot run go: {error}") from error
            if result.returncode != 0:
                raise RenderError(
                    f"prepared cache for {renderer} {tool['pin']} cannot run offline "
                    f"(exit code {result.returncode}); rendering reuses only this cache: "
                    f"{_sanitize_diagnostics(result.stderr, [])}"
                )
    finally:
        _remove_renderer_home(home)

    provenance = {
        "needed": needed,
        "tools": tools,
        "go": {
            "observed": go_version,
            "minimum": ".".join(str(part) for part in max(GO_MINIMUMS[r] for r in needed)),
        },
        "cache": {
            "root": str(cache),
            "gomodcache": str(cache / "gomodcache"),
            "gocache": str(cache / "gocache"),
            "gopath": str(cache / "gopath"),
        },
        "provider_content_rendered": False,
    }
    try:
        (cache / "prepared.json").write_text(
            json.dumps(
                {
                    "tools": {tool["renderer"]: tool["pin"] for tool in tools},
                    "go": go_version,
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise RenderError(f"cannot record preparation provenance in {cache}: {error}") from error
    return provenance


# --- Offline rendering --------------------------------------------------------


def _render_with_go(renderer: str, argv: list[str], scenario: Mapping[str, Any],
                    cache_dir: Path, label: str, secrets: Sequence[str],
                    private_roots: Sequence[str] = ()) -> bytes:
    """Run the pinned go-run command twice offline and prove determinism."""
    scenario_id = scenario.get("id", "<unnamed>")
    home = _new_renderer_home()
    try:
        env = _tool_env(cache_dir, home, offline=True)
        _check_toolchain({renderer}, env, cwd=home)
    finally:
        _remove_renderer_home(home)
    _verify_prepared_cache(cache_dir, renderer)

    outputs: list[bytes] = []
    for _pass in (1, 2):
        home = _new_renderer_home()
        try:
            env = _tool_env(cache_dir, home, offline=True)
            try:
                # Rendering runs from the isolated renderer home, not the
                # caller's working directory: nothing in the checkout that
                # spawned this process can influence file resolution.
                result = _run(
                    list(argv), env=env, cwd=home, timeout=RENDER_TIMEOUT_SECONDS,
                    stdout_limit=MAX_RENDER_OUTPUT_BYTES + 1,
                )
            except subprocess.TimeoutExpired as error:
                raise RenderError(
                    f"scenario {scenario_id!r}: {renderer} rendering of {label} exceeded the "
                    f"{RENDER_TIMEOUT_SECONDS}-second timeout"
                ) from error
            except OSError as error:
                raise RenderError(
                    f"scenario {scenario_id!r}: cannot execute the pinned {renderer} "
                    f"command: {error}"
                ) from error
        finally:
            _remove_renderer_home(home)
        if len(result.stdout) > MAX_RENDER_OUTPUT_BYTES:
            raise RenderError(
                f"scenario {scenario_id!r}: {renderer} rendering of {label} produced more "
                f"than {MAX_RENDER_OUTPUT_BYTES // (1024 * 1024)} MiB of output"
            )
        if result.returncode != 0:
            raise RenderError(
                f"scenario {scenario_id!r}: {renderer} rendering of {label} failed "
                f"(exit code {result.returncode}): "
                f"{_sanitize_diagnostics(result.stderr, secrets, (*private_roots, str(home)))}"
            )
        outputs.append(result.stdout)

    first = _canonical_documents(outputs[0], f"scenario {scenario_id!r} {renderer} pass 1")
    second = _canonical_documents(outputs[1], f"scenario {scenario_id!r} {renderer} pass 2")
    if first != second:
        raise RenderError(
            f"scenario {scenario_id!r}: {renderer} rendering of {label} is not deterministic; "
            "two identical passes produced different output"
        )
    if len(first) > MAX_RENDER_DOCUMENTS:
        raise RenderError(
            f"scenario {scenario_id!r}: rendered output exceeds the "
            f"{MAX_RENDER_DOCUMENTS}-document limit"
        )
    return outputs[0]


def _validate_repo_relative(value: Any, what: str, *, allow_glob: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise RenderError(f"{what} must be a nonempty repo-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise RenderError(f"{what} {value!r} must be a repo-relative path without '..' segments")
    if not allow_glob and _GLOB_CHARS.search(value):
        raise RenderError(f"{what} {value!r} must not contain glob characters")
    # Snapshot keys never carry "./" prefixes or trailing slashes, so inputs
    # are normalized to the shape the snapshot uses; the repository root
    # itself is rejected because no renderer input can be the whole checkout.
    normalized = value
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if normalized in ("", "."):
        raise RenderError(
            f"{what} {value!r} must name a path inside the repository, not the repository root"
        )
    return normalized


def _reject_hazardous_ancestors(snapshot: _Snapshot, rel: str, what: str) -> None:
    """Fail closed when ``rel`` or any of its ancestors is a snapshot hazard.

    A gitlink, symlink, or LFS pointer at an ancestor of a selected input
    hides everything beneath it: those descendant paths match no snapshot
    files, so the input looks wholly absent even though its real content is
    merely uninspectable. Absence classification — and the baseline
    authorization built on it — must never see such a path as absent; it is
    an integrity error instead. The path itself is included, so this also
    covers a hazard at the exact selected path.
    """
    parts = PurePosixPath(rel).parts
    for depth in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:depth])
        hazard = snapshot.hazards.get(ancestor)
        if hazard is not None:
            raise RenderError(
                f"{what} {ancestor!r} cannot be safely materialized "
                f"({hazard.get('hazard')}); its contents cannot be inspected"
            )


def _glob_tokens(pattern: str) -> list[tuple[str, str]]:
    """Split an fnmatch pattern into literal/star/any/class tokens."""
    tokens: list[tuple[str, str]] = []
    literal: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if literal:
                tokens.append(("lit", "".join(literal)))
                literal = []
            tokens.append(("star", ""))
            index += 1
        elif char == "?":
            if literal:
                tokens.append(("lit", "".join(literal)))
                literal = []
            tokens.append(("any", ""))
            index += 1
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close == -1:
                literal.append(char)  # unclosed bracket is a literal, as in fnmatch
                index += 1
                continue
            if literal:
                tokens.append(("lit", "".join(literal)))
                literal = []
            tokens.append(("class", pattern[index:close + 1]))
            index = close + 1
        else:
            literal.append(char)
            index += 1
    if literal:
        tokens.append(("lit", "".join(literal)))
    return tokens


def _glob_could_match_under(pattern: str, prefix: str) -> bool:
    """Whether any string starting with ``prefix`` matches the glob ``pattern``.

    Exact decision for the fnmatch syntax: the pattern's tokens must be able
    to consume ``prefix``, stopping anywhere — a token left partially
    consumed at the end of ``prefix`` is completed by the arbitrary suffix,
    and every fnmatch token is satisfiable by some string. Used to detect
    snapshot hazards whose hidden descendants could have matched a source
    pattern (the hazard itself matching the pattern exactly is checked
    separately): a hazard that could contain matches must never let the
    source set look absent.
    """
    tokens = _glob_tokens(pattern)
    memo: dict[tuple[int, int], bool] = {}

    def consume(index: int, position: int) -> bool:
        state = (index, position)
        if state in memo:
            return memo[state]
        if position == len(prefix):
            result = True
        elif index == len(tokens):
            result = False
        else:
            kind, value = tokens[index]
            if kind == "star":
                result = any(
                    consume(index + 1, nxt)
                    for nxt in range(position, len(prefix) + 1)
                )
            elif kind == "any":
                result = consume(index + 1, position + 1)
            elif kind == "class":
                result = (
                    fnmatch.fnmatchcase(prefix[position], value)
                    and consume(index + 1, position + 1)
                )
            else:
                run = 0
                while (run < len(value) and position + run < len(prefix)
                       and value[run] == prefix[position + run]):
                    run += 1
                if position + run == len(prefix):
                    result = True  # suffix completes the literal
                elif run == len(value):
                    result = consume(index + 1, position + len(value))
                else:
                    result = False
        memo[state] = result
        return result

    return consume(0, 0)


def _render_raw(snapshot: _Snapshot, scenario: Mapping[str, Any]) -> bytes:
    scenario_id = scenario.get("id", "<unnamed>")
    sources = scenario.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RenderError(f"scenario {scenario_id!r}: raw scenarios require a sources list")
    matched_files: list[str] = []
    missing: list[str] = []
    for pattern in sources:
        # The normalized form (no "./" prefixes or trailing slashes) is what
        # snapshot keys use, so both matching and missing-input reporting use
        # it; the checker's scenario inventory normalizes identically.
        pattern = _validate_repo_relative(
            pattern, f"scenario {scenario_id!r} sources entry", allow_glob=True
        )
        # A hazard that matches the pattern exactly cannot be materialized;
        # a hazard whose hidden descendants could match the pattern makes the
        # source set uninspectable — never an absent (authorizable) set. The
        # under-check is exact: it subsumes ancestors of the static prefix
        # and provably unreachable hazards stay legitimate absence.
        for hazard_path in sorted(snapshot.hazards):
            hazard = snapshot.hazards[hazard_path]
            if fnmatch.fnmatchcase(hazard_path, pattern):
                raise RenderError(
                    f"scenario {scenario_id!r}: matched input {hazard_path!r} cannot be "
                    f"safely materialized ({hazard.get('hazard')})"
                )
            if _glob_could_match_under(pattern, hazard_path + "/"):
                raise RenderError(
                    f"scenario {scenario_id!r}: input {hazard_path!r} cannot be "
                    f"inspected ({hazard.get('hazard')}); files it hides could "
                    f"match source pattern {pattern!r}"
                )
        hits = sorted(
            file for file in snapshot.files if fnmatch.fnmatchcase(file, pattern)
        )
        if hits:
            matched_files.extend(hits)
        else:
            missing.append(pattern)
    if missing:
        raise MissingInputError(
            f"scenario {scenario_id!r}: no files match source pattern(s) "
            f"{', '.join(repr(pattern) for pattern in missing)} in the snapshot",
            missing=missing,
            matched=sorted(set(matched_files)),
        )
    parts: list[bytes] = []
    for file in sorted(set(matched_files)):
        content = snapshot.read(file)
        parts.append(content if content.endswith(b"\n") or not content else content + b"\n")
    joined = b"---\n".join(parts)
    if len(joined) > MAX_RENDER_OUTPUT_BYTES:
        raise RenderError(
            f"scenario {scenario_id!r}: raw sources exceed the "
            f"{MAX_RENDER_OUTPUT_BYTES // (1024 * 1024)} MiB output limit"
        )
    documents = _canonical_documents(joined, f"scenario {scenario_id!r} raw sources")
    if len(documents) > MAX_RENDER_DOCUMENTS:
        raise RenderError(
            f"scenario {scenario_id!r}: raw sources exceed the "
            f"{MAX_RENDER_DOCUMENTS}-document limit"
        )
    return joined


def _render_helm(snapshot: _Snapshot, scenario: Mapping[str, Any], cache_dir: Path) -> bytes:
    scenario_id = scenario.get("id", "<unnamed>")
    chart_rel = _validate_repo_relative(
        scenario.get("path"), f"scenario {scenario_id!r} chart path"
    )
    chart_prefix = chart_rel.strip("/") + "/"
    # Classify the chart path and every ancestor before its descendants: a
    # gitlink, symlink, or LFS pointer at the root or any ancestor is a
    # hazard (uninspectable content, never an authorizable absent chart),
    # and a root that is a regular file is a wrong-kind input.
    _reject_hazardous_ancestors(
        snapshot, chart_rel, f"scenario {scenario_id!r} chart path",
    )
    if chart_rel in snapshot.files:
        raise RenderError(
            f"scenario {scenario_id!r}: chart path {chart_rel!r} is a regular file, "
            "not a chart directory"
        )
    chart_hazards = sorted(file for file in snapshot.hazards if file.startswith(chart_prefix))
    if chart_hazards:
        hazard = snapshot.hazards[chart_hazards[0]]
        raise RenderError(
            f"scenario {scenario_id!r}: chart {chart_rel!r} contains input "
            f"{chart_hazards[0]!r} that cannot be safely materialized "
            f"({hazard.get('hazard')})"
        )
    # Classify every selected values file before declaring the chart absent:
    # a hazardous one is malformed regardless of the chart, and a present one
    # makes the input set partial rather than wholly missing.
    values_present: list[str] = []
    values_missing: list[str] = []
    for entry in scenario.get("valuesFiles") or []:
        rel = _validate_repo_relative(entry, f"scenario {scenario_id!r} values file")
        _reject_hazardous_ancestors(
            snapshot, rel, f"scenario {scenario_id!r} values file",
        )
        if rel in snapshot.files:
            values_present.append(rel)
        else:
            values_missing.append(rel)
    if not any(file.startswith(chart_prefix) for file in snapshot.files):
        if values_present:
            raise MissingInputError(
                f"scenario {scenario_id!r}: chart path {chart_rel!r} is absent from the "
                f"snapshot but selected values files are present "
                f"({', '.join(values_present)})",
                missing=[chart_rel],
                matched=values_present,
            )
        raise MissingInputError(
            f"scenario {scenario_id!r}: chart path {chart_rel!r} is absent from the snapshot",
            missing=[chart_rel, *values_missing],
            matched=[],
        )
    if f"{chart_rel}/Chart.yaml" not in snapshot.files:
        raise RenderError(f"scenario {scenario_id!r}: chart {chart_rel!r} has no Chart.yaml")

    _scan_chart_tree(_RecordTree(snapshot, chart_prefix), chart_rel, 0)

    release_name = scenario.get("releaseName")
    if (
        not isinstance(release_name, str)
        or len(release_name) > HELM_RELEASE_NAME_MAX_LENGTH
        or _HELM_RELEASE_NAME_RE.fullmatch(release_name) is None
    ):
        raise RenderError(
            f"scenario {scenario_id!r}: releaseName must be a Helm release name "
            f"(lowercase alphanumerics, '-' and '.', starting and ending with an "
            f"alphanumeric, at most {HELM_RELEASE_NAME_MAX_LENGTH} characters)"
        )
    namespace = scenario.get("namespace")
    kubernetes_version = scenario.get("kubernetesVersion")
    if not isinstance(namespace, str) or not namespace:
        raise RenderError(f"scenario {scenario_id!r}: scenario requires a namespace")
    if not isinstance(kubernetes_version, str) or not kubernetes_version:
        raise RenderError(f"scenario {scenario_id!r}: scenario requires kubernetesVersion")

    if values_missing:
        raise MissingInputError(
            f"scenario {scenario_id!r}: values file(s) "
            f"{', '.join(repr(rel) for rel in values_missing)} are absent from the snapshot",
            missing=values_missing,
            matched=[chart_rel],
        )
    values_rels = values_present
    set_values = scenario.get("setValues") or {}
    if not isinstance(set_values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in set_values.items()
    ):
        raise RenderError(f"scenario {scenario_id!r}: setValues must map strings to strings")
    api_versions = scenario.get("apiVersions") or []
    if not isinstance(api_versions, list) or not all(
        isinstance(version, str) and version for version in api_versions
    ):
        raise RenderError(f"scenario {scenario_id!r}: apiVersions must be a list of strings")
    release_mode = scenario.get("releaseMode", "upgrade")
    if release_mode not in ("upgrade", "install"):
        raise RenderError(f"scenario {scenario_id!r}: releaseMode must be upgrade or install")

    secrets = _helm_secret_markers(snapshot, chart_prefix, values_rels, set_values)
    if snapshot.root is None:
        raise RenderError(f"scenario {scenario_id!r}: snapshot has no root")
    chart_dir = Path(snapshot.root) / chart_rel
    # Every supported option precedes the "--" terminator; the release name and
    # chart path are positionals after it, so a release name can never be
    # re-parsed as a flag even if some future validation gap let one through.
    argv: list[str] = [
        "go", "run", HELM_PIN, "template",
        "--dry-run=client",
        "--namespace", namespace,
        "--kube-version", kubernetes_version,
        "--include-crds",
        "--skip-schema-validation",
    ]
    for version in api_versions:
        argv.extend(["--api-versions", version])
    for rel in values_rels:
        argv.extend(["--values", str(Path(snapshot.root) / rel)])
    for key in sorted(set_values):
        argv.extend(["--set-string", f"{key}={set_values[key]}"])
    if release_mode == "upgrade":
        argv.append("--is-upgrade")
    argv.extend(["--", release_name, str(chart_dir)])
    return _render_with_go(
        "helm", argv, scenario, cache_dir, f"chart {chart_rel!r}", secrets,
        private_roots=() if snapshot.root is None else (str(snapshot.root),),
    )


def _is_inline_patch(entry: str) -> bool:
    """Whether a patchesStrategicMerge entry is inline patch content.

    The pinned kustomize parses each entry as YAML patch content first and
    treats it as a file path only when that fails, so any entry that parses
    to a mapping is inline content. Relative path strings parse as scalars
    and therefore stay file references.
    """
    try:
        parsed = yaml.safe_load(entry)
    except yaml.YAMLError:
        return False
    return isinstance(parsed, dict)


def _resolve_local_reference(entry: str, base_dir: str, snapshot: _Snapshot, field: str,
                             where: str, *, require_file: bool = False) -> str:
    """Resolve one kustomize reference inside the snapshot, fail closed."""
    if not entry:
        raise RenderError(f"{where}: empty {field} entry")
    if _REMOTE_REFERENCE_RE.search(entry) or entry.startswith("git@"):
        raise RenderError(
            f"{where}: {field} entry {entry!r} is a remote reference; only local paths "
            "inside the repository are supported"
        )
    if PurePosixPath(entry).is_absolute():
        raise RenderError(f"{where}: {field} entry {entry!r} must be a relative path")
    if snapshot.root is None:
        raise MissingInputError(
            f"{where}: {field} entry {entry!r} does not exist in the snapshot",
            missing=[entry], matched=[],
        )
    root = Path(snapshot.root)
    resolved = Path(os.path.normpath(root / base_dir / entry))
    if not resolved.is_relative_to(root):
        raise RenderError(
            f"{where}: {field} entry {entry!r} escapes the repository snapshot"
        )
    rel = resolved.relative_to(root).as_posix()
    if rel in snapshot.hazards:
        raise RenderError(
            f"{where}: {field} entry {entry!r} cannot be safely materialized "
            f"({snapshot.hazards[rel].get('hazard')})"
        )
    if rel in snapshot.files:
        return rel
    if rel in snapshot.directories:
        if require_file:
            raise RenderError(f"{where}: {field} entry {entry!r} must be a file, not a directory")
        return rel
    # The kustomization being validated exists in the snapshot, so this is a
    # partially present input set, not an absent one: a plain render error
    # keeps it comparable (and refuses a false explicit-new declaration)
    # instead of surfacing as "inputs absent".
    raise RenderError(
        f"{where}: {field} entry {entry!r} does not exist in the snapshot"
    )


def _resolve_glob_files(entry: str, base_dir: str, snapshot: _Snapshot, field: str,
                        where: str) -> list[str]:
    prefix = base_dir.strip("/") + "/"
    hits = sorted(
        file for file in snapshot.files
        if file.startswith(prefix) and fnmatch.fnmatchcase(file[len(prefix):], entry)
    )
    hazards = sorted(
        file for file in snapshot.hazards
        if file.startswith(prefix) and fnmatch.fnmatchcase(file[len(prefix):], entry)
    )
    if hazards:
        raise RenderError(
            f"{where}: {field} entry {entry!r} matches input {hazards[0]!r} that cannot be "
            f"safely materialized ({snapshot.hazards[hazards[0]].get('hazard')})"
        )
    if not hits:
        # Same partial-presence rule as _resolve_local_reference: the
        # kustomization exists, so a glob that matches nothing is a broken
        # historical input, not an absent source set.
        raise RenderError(
            f"{where}: {field} entry {entry!r} matches no files in the snapshot"
        )
    return hits


def _plugin_spec_documents(content: str | bytes, source: str) -> list[dict[str, Any]]:
    """Parse a plugin configuration stream the way the pinned kustomize does.

    Every document in a ``generators``/``transformers``/``validators`` entry
    is configured as a plugin, and any document whose kind ends in ``List``
    is expanded into its items (resource/factory.go ``inlineAnyEmbeddedLists``).
    Empty documents are ignored; anything else must be a mapping.
    """
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        parsed = list(yaml.safe_load_all(text))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise RenderError(
            f"{source}: plugin configuration is not valid YAML ({type(error).__name__})"
        ) from error
    specs: list[dict[str, Any]] = []
    for document in parsed:
        if document is None:
            continue
        if not isinstance(document, dict):
            raise RenderError(f"{source}: plugin configurations must be YAML mappings")
        kind = document.get("kind")
        items = document.get("items")
        if isinstance(kind, str) and kind.endswith("List") and isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    raise RenderError(f"{source}: list items must be YAML mappings")
                specs.append(item)
            continue
        specs.append(document)
    return specs


def _validate_plugin_input(entry: str, field: str, source: str,
                           kust_dir: str, snapshot: _Snapshot) -> None:
    """Resolve one file reference from a plugin configuration, fail closed.

    References inside plugin configurations resolve through the *invoking
    kustomization's* loader — relative to ``kust_dir``, the directory of the
    kustomization that declared the entry — not the configuration file's own
    directory (pinned kustomize: kusttarget_configplugin.go hands every
    plugin's Config call the kustomization's loader). Literal data is never
    routed through here, so URL-like literal values stay legal.
    """
    if _GLOB_CHARS.search(entry):
        _resolve_glob_files(entry, kust_dir, snapshot, field, source)
    else:
        _resolve_local_reference(
            entry, kust_dir, snapshot, field, source, require_file=True
        )


def _validate_generator_sources(entry: Mapping[str, Any], label: str,
                                kust_dir: str, snapshot: _Snapshot) -> None:
    """Validate the files/envs/env reference fields of one generator entry.

    Shared by kustomization-level ``configMapGenerator``/``secretGenerator``
    entries and builtin generator plugin configurations. File sources accept
    ``key=path`` aliases (generators.ParseFileSource splits on the single
    ``=``); env sources reject ``=`` themselves; the deprecated singular
    ``env`` (merged into ``envs`` by the pinned kustomize) holds one plain
    file path.
    """
    for key in ("files", "envs"):
        values = entry.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise RenderError(f"{label}: {key} entries must be strings")
        for value in values:
            if key == "files" and "=" in value:
                path_value = value.split("=", 1)[1]
            else:
                path_value = value
            _validate_plugin_input(path_value, key, label, kust_dir, snapshot)
    env = entry.get("env")
    if env is not None:
        if not isinstance(env, str) or not env:
            raise RenderError(f"{label}: env must be a nonempty string")
        _validate_plugin_input(env, "env", label, kust_dir, snapshot)


def _validate_plugin_spec(spec: Mapping[str, Any], source: str,
                          kust_dir: str, snapshot: _Snapshot) -> None:
    """Validate one builtin plugin configuration, rejecting unsafe forms."""
    _reject_case_folded_keys(spec, _PLUGIN_SPEC_FIELDS, source)
    api_version = spec.get("apiVersion")
    kind = spec.get("kind")
    if isinstance(api_version, str) and isinstance(kind, str):
        combined = f"{api_version} {kind}"
        for forbidden in ("exec", "container"):
            if re.search(rf"\b{forbidden}\b", combined, re.IGNORECASE):
                raise RenderError(
                    f"{source}: {forbidden} plugins are not supported; kustomize rendering "
                    "is restricted to local file references and built-in transformers"
                )
    if api_version != _BUILTIN_PLUGIN_API_VERSION or not isinstance(kind, str) or not kind:
        raise RenderError(
            f"{source}: only builtin plugin configurations (apiVersion "
            f"{_BUILTIN_PLUGIN_API_VERSION!r}) are supported; external, exec, and "
            "container plugin configurations are rejected before any subprocess runs"
        )
    if kind == "HelmChartInflationGenerator":
        raise RenderError(
            f"{source}: Helm-in-Kustomize (HelmChartInflationGenerator) is not supported; "
            "render the chart with the helm renderer instead"
        )
    fields = _PLUGIN_REFERENCE_FIELDS.get(kind)
    if fields is None:
        if kind not in _REFERENCE_FREE_PLUGIN_KINDS:
            raise RenderError(
                f"{source}: builtin plugin kind {kind!r} is not supported; rendering "
                "supports the builtin transformers and generators of the pinned kustomize"
            )
        return
    if kind in ("ConfigMapGenerator", "SecretGenerator"):
        _validate_generator_sources(spec, source, kust_dir, snapshot)
        return
    for field, shape in fields.items():
        value = spec.get(field)
        if value is None:
            continue
        if shape == "file":
            if not isinstance(value, str) or not value:
                raise RenderError(f"{source}: {field} must be a nonempty string")
            _validate_plugin_input(value, field, source, kust_dir, snapshot)
        elif shape == "files":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise RenderError(f"{source}: {field} must be a list of strings")
            for item in value:
                _validate_plugin_input(item, field, source, kust_dir, snapshot)
        elif shape == "replacements":
            if not isinstance(value, list):
                raise RenderError(f"{source}: {field} must be a list")
            for entry in value:
                if not isinstance(entry, dict):
                    raise RenderError(f"{source}: {field} entries must be mappings")
                _reject_case_folded_keys(
                    entry, _KUSTOMIZATION_ENTRY_FIELDS["replacements"],
                    f"{source}: {field} entry",
                )
                path = entry.get("path")
                if path is None:
                    continue
                if not isinstance(path, str) or not path:
                    raise RenderError(f"{source}: {field} path must be a nonempty string")
                _validate_plugin_input(path, f"{field} path", source, kust_dir, snapshot)


def _validate_plugin_config(content: str | bytes, source: str,
                            kust_dir: str, snapshot: _Snapshot) -> None:
    """Validate every plugin spec in one generators/transformers/validators entry.

    ``content`` is either an inline entry from the kustomization itself or a
    referenced configuration file's bytes; every document in the stream is
    checked, not just the first. Unrecognized or non-builtin plugin forms
    fail actionably instead of bypassing validation.
    """
    for spec in _plugin_spec_documents(content, source):
        _validate_plugin_spec(spec, source, kust_dir, snapshot)


def _validate_kustomization_tree(snapshot: _Snapshot, kust_dir: str,
                                 visited: set[str]) -> None:
    if kust_dir in visited:
        return
    visited.add(kust_dir)
    kust_file = next(
        (name for name in _KUSTOMIZATION_NAMES if f"{kust_dir}/{name}" in snapshot.files), None
    )
    if kust_file is None:
        hazard = next(
            (name for name in _KUSTOMIZATION_NAMES if f"{kust_dir}/{name}" in snapshot.hazards),
            None,
        )
        if hazard is not None:
            raise RenderError(
                f"kustomization {kust_dir}/{hazard} cannot be safely materialized "
                f"({snapshot.hazards[f'{kust_dir}/{hazard}'].get('hazard')})"
            )
        raise RenderError(f"{kust_dir}: directory is not a kustomization root")
    source = f"{kust_dir}/{kust_file}"
    document = _parse_yaml_document(snapshot.read(source), source)
    _reject_case_folded_keys(document, _KUSTOMIZATION_FIELDS, source)
    for forbidden in ("helmGlobals", "helmCharts", "helmChart"):
        if forbidden in document:
            raise RenderError(
                f"{source}: Helm-in-Kustomize ({forbidden}) is not supported; render the "
                "chart with the helm renderer instead"
            )
    where = source
    for field in ("resources", "bases", "components"):
        entries = document.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise RenderError(f"{where}: {field} must be a list")
        for entry in entries:
            if not isinstance(entry, str):
                raise RenderError(f"{where}: {field} entries must be strings")
            resolved = _resolve_local_reference(entry, kust_dir, snapshot, field, where)
            if resolved in snapshot.directories:
                _validate_kustomization_tree(snapshot, resolved, visited)
    # crds and configurations hold data files kustomize consumes itself
    # (CRD manifests, FieldSpec configuration); they are not plugin
    # configurations and carry no nested reference fields, so entries are
    # plain file paths or globs.
    for field in ("crds", "configurations"):
        entries = document.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise RenderError(f"{where}: {field} must be a list")
        for entry in entries:
            if not isinstance(entry, str):
                raise RenderError(f"{where}: {field} entries must be strings")
            if "\n" in entry:
                raise RenderError(f"{where}: {field} entries must be file paths")
            if _GLOB_CHARS.search(entry):
                _resolve_glob_files(entry, kust_dir, snapshot, field, where)
            else:
                _resolve_local_reference(
                    entry, kust_dir, snapshot, field, where, require_file=True
                )
    # generators, transformers, and validators hold plugin configurations:
    # inline entries, referenced configuration files, or globs of them. The
    # pinned kustomize configures every document in each entry as a plugin
    # through the invoking kustomization's loader, so both the entry paths
    # and the references nested inside the configurations are validated —
    # the configuration file's own location is never the resolution base.
    for field in ("generators", "transformers", "validators"):
        entries = document.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise RenderError(f"{where}: {field} must be a list")
        for entry in entries:
            if not isinstance(entry, str):
                raise RenderError(f"{where}: {field} entries must be strings")
            if "\n" in entry:
                _validate_plugin_config(entry, where, kust_dir, snapshot)
            elif entry.startswith("builtin:"):
                continue
            elif _GLOB_CHARS.search(entry):
                for rel in _resolve_glob_files(entry, kust_dir, snapshot, field, where):
                    _validate_plugin_config(
                        snapshot.read(rel), rel, kust_dir, snapshot
                    )
            else:
                rel = _resolve_local_reference(
                    entry, kust_dir, snapshot, field, where, require_file=True
                )
                _validate_plugin_config(snapshot.read(rel), rel, kust_dir, snapshot)

    patches = document.get("patches")
    if patches is not None:
        if not isinstance(patches, list):
            raise RenderError(f"{where}: patches must be a list")
        for entry in patches:
            if isinstance(entry, str):
                _resolve_local_reference(entry, kust_dir, snapshot, "patches", where, require_file=True)
            elif isinstance(entry, dict):
                _reject_case_folded_keys(
                    entry, _KUSTOMIZATION_ENTRY_FIELDS["patches"], f"{where}: patches entry",
                )
                path = entry.get("path")
                if isinstance(path, str) and path:
                    _resolve_local_reference(path, kust_dir, snapshot, "patches", where, require_file=True)
            else:
                raise RenderError(f"{where}: patches entries must be strings or mappings")
    # Deprecated but still supported by kustomize; the pinned kustomize
    # fetches URLs named here, so file references must be validated like
    # every other field (offline rendering must have no network path at
    # all). Entries that parse as YAML mappings are inline patch content,
    # which kustomize itself accepts ("for legacy reasons" its
    # PatchStrategicMergeTransformer parses each entry as a patch first and
    # falls back to treating it as a file path); inline content is never a
    # reference and is never echoed by our errors.
    patches_sm = document.get("patchesStrategicMerge")
    if patches_sm is not None:
        if not isinstance(patches_sm, list):
            raise RenderError(f"{where}: patchesStrategicMerge must be a list")
        for entry in patches_sm:
            if not isinstance(entry, str):
                raise RenderError(f"{where}: patchesStrategicMerge entries must be strings")
            if _is_inline_patch(entry):
                continue
            _resolve_local_reference(
                entry, kust_dir, snapshot, "patchesStrategicMerge", where, require_file=True
            )
    for field, keys in (
        ("patchesJson6902", ("path",)),
        ("replacements", ("path",)),
        ("labels", ("pairs",)),
    ):
        entries = document.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise RenderError(f"{where}: {field} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RenderError(f"{where}: {field} entries must be mappings")
            _reject_case_folded_keys(
                entry, _KUSTOMIZATION_ENTRY_FIELDS[field], f"{where}: {field} entry",
            )
            for key in keys:
                path = entry.get(key)
                if isinstance(path, str) and path:
                    _resolve_local_reference(path, kust_dir, snapshot, key, where, require_file=True)
    openapi = document.get("openapi")
    if isinstance(openapi, dict):
        path = openapi.get("path")
        if isinstance(path, str) and path:
            _resolve_local_reference(path, kust_dir, snapshot, "openapi", where, require_file=True)
    for field in ("configMapGenerator", "secretGenerator"):
        entries = document.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise RenderError(f"{where}: {field} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RenderError(f"{where}: {field} entries must be mappings")
            _reject_case_folded_keys(
                entry, _KUSTOMIZATION_ENTRY_FIELDS[field], f"{where}: {field} entry",
            )
            _validate_generator_sources(entry, f"{where}: {field}", kust_dir, snapshot)


def _render_kustomize(snapshot: _Snapshot, scenario: Mapping[str, Any],
                      cache_dir: Path) -> bytes:
    scenario_id = scenario.get("id", "<unnamed>")
    kust_rel = _validate_repo_relative(
        scenario.get("path"), f"scenario {scenario_id!r} kustomize path"
    )
    kust_prefix = kust_rel.strip("/") + "/"
    # Classify the overlay path and every ancestor before its descendants: a
    # gitlink, symlink, or LFS pointer at the root or any ancestor is a
    # hazard (uninspectable content, never an authorizable absent overlay),
    # and a root that is a regular file is a wrong-kind input.
    _reject_hazardous_ancestors(
        snapshot, kust_rel, f"scenario {scenario_id!r} kustomize path",
    )
    if kust_rel in snapshot.files:
        raise RenderError(
            f"scenario {scenario_id!r}: kustomize path {kust_rel!r} is a regular file, "
            "not a kustomization directory"
        )
    for name in _KUSTOMIZATION_NAMES:
        rel = f"{kust_rel}/{name}"
        if rel in snapshot.hazards:
            raise RenderError(
                f"scenario {scenario_id!r}: kustomization file {rel!r} cannot be safely "
                f"materialized ({snapshot.hazards[rel].get('hazard')})"
            )
    if not any(file.startswith(kust_prefix) for file in snapshot.files):
        raise MissingInputError(
            f"scenario {scenario_id!r}: kustomize path {kust_rel!r} is absent from the snapshot",
            missing=[kust_rel],
            matched=[],
        )
    if not any(f"{kust_rel}/{name}" in snapshot.files for name in _KUSTOMIZATION_NAMES):
        raise RenderError(
            f"scenario {scenario_id!r}: {kust_rel!r} contains no kustomization file"
        )
    _validate_kustomization_tree(snapshot, kust_rel, set())
    if snapshot.root is None:
        raise RenderError(f"scenario {scenario_id!r}: snapshot has no root")
    argv = ["go", "run", KUSTOMIZE_PIN, "build", str(Path(snapshot.root) / kust_rel)]
    secrets = _kustomize_secret_markers(snapshot, kust_prefix)
    return _render_with_go(
        "kustomize", argv, scenario, cache_dir, f"kustomization {kust_rel!r}", secrets,
        private_roots=(str(snapshot.root),),
    )


def render(snapshot: Sequence[Mapping[str, Any]], scenario: Mapping[str, Any],
          cache_dir: Path | None) -> bytes:
    """Render one revision described by ``snapshot`` according to ``scenario``.

    ``snapshot`` is the record list returned by ``snapshot_candidate`` or
    ``snapshot_baseline``. Raw scenarios need neither Go nor a tool cache;
    Helm and Kustomize scenarios require a prepared cache directory and run
    the pinned ``go run`` commands offline, twice, in clean renderer homes.
    A successful render that produces zero documents returns ``b""`` and is
    valid for comparison; absent inputs raise ``MissingInputError`` instead.
    """
    if not isinstance(scenario, Mapping):
        raise RenderError("scenario must be a mapping")
    renderer = scenario.get("renderer")
    parsed = _Snapshot(snapshot)
    if renderer == "raw":
        return _render_raw(parsed, scenario)
    if renderer not in ("helm", "kustomize"):
        raise RenderError(f"unsupported renderer {renderer!r}")
    if cache_dir is None:
        raise RenderError(
            f"scenario {scenario.get('id', '<unnamed>')!r}: rendering with "
            f"{renderer!r} requires a prepared tool cache directory"
        )
    cache = Path(cache_dir)
    if renderer == "helm":
        return _render_helm(parsed, scenario, cache)
    return _render_kustomize(parsed, scenario, cache)
