#!/usr/bin/env python3
"""Prepare, reconcile, validate, publish, and report one native nightly candidate."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import benchmark_contract
import privacy_gate


class NightlyError(RuntimeError):
    pass


PUBLIC_UPSTREAM = "https://github.com/manaflow-ai/cmux.git"
INTEGRATION_BRANCH = "nightly-integration"
REPORT_PATH = Path("integration/nightly/report.md")
STATE_PATH = Path("integration/nightly/benchmark-state.json")
MANIFEST_PATH = Path("integration/nightly/pr-manifest.json")
WORKFLOW_MANIFEST_PATH = Path("integration/nightly/workflows.json")
PRODUCT_MANIFEST_PATH = Path("integration/nightly/product-files.txt")
PAIR_STATE_PATH = Path(".integration-results/nightly-pair.json")
CANDIDATE_PATH = Path(".integration-results/candidate")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_HTTP_TIMEOUT_SECONDS = 30.0
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PAIR_SCHEMA = 2
_GHOSTTY_UPSTREAM = "https://github.com/manaflow-ai/ghostty.git"
_PUBLIC_GIT_NAME = "cmux integration"
_PUBLIC_GIT_EMAIL = "cmux-integration@users.noreply.github.com"
_ROYGBIV_GIT_NAME = "usr-bin-roygbiv"
_ROYGBIV_GIT_EMAIL = "282155169+usr-bin-roygbiv@users.noreply.github.com"
_ISOLATED_GIT_CONFIG_ARGS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
    "-c",
    f"user.name={_PUBLIC_GIT_NAME}",
    "-c",
    f"user.email={_PUBLIC_GIT_EMAIL}",
)
_NORMALIZE_AUTHOR_CODE = (
    "import subprocess;"
    "name, email = subprocess.check_output("
    "['git', 'show', '-s', '--format=%an%x00%ae', 'HEAD'], text=True).strip().split('\\0', 1);"
    f"author = None if email.lower().endswith('@users.noreply.github.com') else "
    f"({_ROYGBIV_GIT_NAME + ' <' + _ROYGBIV_GIT_EMAIL + '>'!r} "
    f"if name == {_ROYGBIV_GIT_NAME!r} else "
    f"{_PUBLIC_GIT_NAME + ' <' + _PUBLIC_GIT_EMAIL + '>'!r});"
    f"subprocess.run(['git', *{_ISOLATED_GIT_CONFIG_ARGS!r}, 'commit', '--amend', '--no-edit', "
    "'--no-verify', '--no-gpg-sign', '--allow-empty', '--author=' + author], check=True) "
    "if author is not None else None"
)


class Operations(Protocol):
    def fetch_upstream(self) -> str: ...
    def create_rebased_candidate(self, baseline_sha: str) -> str: ...
    def run_omp(self, candidate_sha: str) -> str: ...
    def privacy_gate(self, baseline_sha: str, candidate_sha: str) -> None: ...
    def validate_product(self, candidate_sha: str) -> None: ...
    def push_candidate(self, candidate_sha: str) -> None: ...


def prepare_candidate(operations: Operations) -> str:
    """Retain the observable failure-before-push ordering contract."""
    baseline_sha = operations.fetch_upstream()
    candidate_sha = operations.create_rebased_candidate(baseline_sha)
    candidate_sha = operations.run_omp(candidate_sha)
    operations.privacy_gate(baseline_sha, candidate_sha)
    operations.validate_product(candidate_sha)
    operations.push_candidate(candidate_sha)
    return candidate_sha


def _validate_sha(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if _SHA_RE.fullmatch(normalized) is None:
        raise NightlyError(f"{name} must be exactly 40 hexadecimal characters")
    return normalized


def _validate_repository(value: str) -> str:
    if _REPOSITORY_RE.fullmatch(value) is None:
        raise NightlyError("public repository manifest entry must be an owner/name slug")
    return value


def _validate_http_url(value: str, label: str) -> tuple[str, urllib.parse.SplitResult]:
    endpoint = value.strip()
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        hostname = parsed.hostname
    except ValueError as error:
        raise NightlyError(f"{label} is missing or invalid") from error
    if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise NightlyError(f"{label} is missing or invalid")
    return endpoint, parsed


def _agent_preexec(uid: int, gid: int) -> Callable[[], None]:
    def drop_privileges() -> None:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    return drop_privileges


def _redacted_failure_tail(
    error: subprocess.CalledProcessError[str],
    environment: Mapping[str, str] | None,
) -> str:
    parts: list[str] = []
    for stream, output in (("stdout", error.stdout), ("stderr", error.stderr)):
        if not output:
            continue
        lines = [" ".join(line.split()) for line in output.splitlines() if line.strip()]
        recent = lines[-40:]
        source_context = [
            line
            for line in recent
            if line in ("before:", "after:")
            or line.startswith(("?? ", "M ", "A ", "D ", "R ", "C ", "U "))
        ]
        actionable = [
            line
            for line in recent
            if line not in source_context
            and any(
                marker in line.lower()
                for marker in ("assertion", "failed", "failure", "error:", "panicked", "left:", "right:")
            )
        ]
        selected = actionable[-8:] + source_context[-8:]
        detail = " | ".join(selected or lines[-10:])
        if environment is not None:
            for name, value in environment.items():
                normalized = name.upper()
                if value and any(
                    marker in normalized
                    for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
                ):
                    detail = detail.replace(value, "[redacted]")
        encoded = detail.encode("utf-8")
        if len(encoded) > 360:
            detail = "…" + encoded[-360:].decode("utf-8", errors="ignore")
        parts.append(f"{stream}: {detail}")
    return " | ".join(parts)

def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    label: str,
    uid: int | None = None,
    gid: int | None = None,
    strip_output: bool = True,
) -> str:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            env=None if env is None else dict(env),
            input=input_text,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=None if uid is None else _agent_preexec(uid, uid if gid is None else gid),
        )
    except subprocess.CalledProcessError as error:
        detail = _redacted_failure_tail(error, env)
        suffix = f": {detail}" if detail else ""
        raise NightlyError(f"{label} failed with exit code {error.returncode}{suffix}") from error
    except OSError as error:
        raise NightlyError(f"{label} failed") from error
    return completed.stdout.strip() if strip_output else completed.stdout


def _git(
    root: Path,
    args: Sequence[str],
    label: str,
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    strip_output: bool = True,
) -> str:
    return _run(
        ["git", *args],
        cwd=root,
        input_text=input_text,
        env=env,
        label=label,
        strip_output=strip_output,
    )


def isolated_git_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in tuple(environment):
        if name == "EMAIL" or name.startswith("GIT_"):
            environment.pop(name, None)
    environment.update(
        {
            "HOME": "/dev/null",
            "XDG_CONFIG_HOME": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _isolated_git(
    root: Path,
    args: Sequence[str],
    label: str,
    *,
    env: Mapping[str, str] | None = None,
    strip_output: bool = True,
) -> str:
    environment = isolated_git_environment(env)
    return _git(
        root,
        [*_ISOLATED_GIT_CONFIG_ARGS, *args],
        label,
        env=environment,
        strip_output=strip_output,
    )


def create_isolated_candidate(
    control: Path,
    candidate: Path,
    source_sha: str,
    *,
    public_remote: str | None = None,
    branch: str = INTEGRATION_BRANCH,
    expected_lease: str | None = None,
) -> None:
    source = _validate_sha(source_sha, "source_sha")
    _run(
        [
            "git",
            *_ISOLATED_GIT_CONFIG_ARGS,
            "clone",
            "--no-local",
            "--no-checkout",
            "--quiet",
            str(control.resolve()),
            str(candidate.resolve()),
        ],
        cwd=control.parent,
        env=isolated_git_environment(),
        label="isolated candidate clone",
    )
    if public_remote is not None:
        _isolated_git(
            candidate,
            [
                "fetch",
                "--quiet",
                "--no-tags",
                public_remote,
                f"+refs/heads/{branch}:refs/nightly/public-lease",
            ],
            "public lease fetch",
        )
        fetched_lease = _validate_sha(
            _isolated_git(candidate, ["rev-parse", "refs/nightly/public-lease"], "fetched public lease"),
            "lease_sha",
        )
        if expected_lease is None or fetched_lease != _validate_sha(expected_lease, "lease_sha"):
            raise NightlyError("fetched public integration lease changed")
        source_fetch = [
            "fetch",
            "--quiet",
            "--no-tags",
            public_remote,
            f"+{source}:refs/nightly/public-source",
        ]
        if (
            _isolated_git(
                candidate,
                ["rev-parse", "--is-shallow-repository"],
                "candidate shallow state",
            )
            == "true"
        ):
            source_fetch.insert(1, "--unshallow")
        _isolated_git(candidate, source_fetch, "public source fetch")
        fetched_source = _validate_sha(
            _isolated_git(candidate, ["rev-parse", "refs/nightly/public-source"], "fetched public source"),
            "source_sha",
        )
        if fetched_source != source:
            raise NightlyError("fetched public source changed")
    _isolated_git(candidate, ["checkout", "--quiet", "--detach", source], "isolated candidate checkout")


def rebase_candidate(candidate: Path, baseline_sha: str, merge_base_sha: str, source_sha: str) -> str:
    baseline = _validate_sha(baseline_sha, "baseline_sha")
    merge_base = _validate_sha(merge_base_sha, "merge_base")
    source = _validate_sha(source_sha, "source_sha")
    environment = isolated_git_environment()
    environment.update(
        {
            "GIT_COMMITTER_NAME": _PUBLIC_GIT_NAME,
            "GIT_COMMITTER_EMAIL": _PUBLIC_GIT_EMAIL,
        }
    )
    _git(
        candidate,
        [
            *_ISOLATED_GIT_CONFIG_ARGS,
            "rebase",
            "--force-rebase",
            "--no-gpg-sign",
            "--exec",
            shlex.join([sys.executable, "-c", _NORMALIZE_AUTHOR_CODE]),
            "--onto",
            baseline,
            merge_base,
            source,
        ],
        "candidate rebase",
        env=environment,
    )
    return _validate_sha(
        _isolated_git(candidate, ["rev-parse", "HEAD"], "rebased candidate revision"),
        "candidate_sha",
    )


def scan_candidate_commit_metadata(
    candidate: Path,
    baseline_sha: str,
    candidate_sha: str,
    denylist: Iterable[str] = (),
) -> list[privacy_gate.Finding]:
    baseline = _validate_sha(baseline_sha, "baseline_sha")
    head = _validate_sha(candidate_sha, "candidate_sha")
    revision_range = f"{baseline}..{head}"
    metadata = _isolated_git(
        candidate,
        ["log", "--reverse", "--format=%H%n%an%n%ae%n%cn%n%ce%n%B", revision_range, "--"],
        "candidate commit metadata",
    )
    findings = privacy_gate.scan_text(Path("@commit-metadata"), metadata, denylist=denylist)
    emails = _isolated_git(
        candidate,
        ["log", "--reverse", "--format=%ae%n%ce", revision_range, "--"],
        "candidate commit identities",
    )
    for line, email in enumerate(emails.splitlines(), start=1):
        if not email.lower().endswith("@users.noreply.github.com"):
            findings.append(privacy_gate.Finding(path=Path("@commit-metadata"), line=line, rule="email"))
    return sorted(set(findings), key=lambda item: (item.path.as_posix(), item.line, item.rule))


def _load_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NightlyError(f"could not read {path.name}") from error
    if not isinstance(document, dict):
        raise NightlyError(f"{path.name} must contain a JSON object")
    return document


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)

def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists() and not path.is_symlink():
        digest.update(b"missing\0")
        return digest.hexdigest()
    entries = [path] if not path.is_dir() or path.is_symlink() else [path, *sorted(path.rglob("*"))]
    for entry in entries:
        relative = Path(".") if entry == path else entry.relative_to(path)
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        if entry.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(entry).encode("utf-8"))
        elif entry.is_file():
            digest.update(b"file\0")
            digest.update(entry.read_bytes())
        elif entry.is_dir():
            digest.update(b"directory\0")
        digest.update(str(stat.S_IMODE(entry.lstat().st_mode)).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def optional_control_pipeline_hashes(root: Path) -> dict[str, str]:
    control_pipeline = root / ".woodpecker-control.yml"
    if not control_pipeline.exists():
        return {}
    if not control_pipeline.is_file():
        raise NightlyError("private control pipeline path is not a file")
    return {"@control-pipeline": _digest_path(control_pipeline)}


def trusted_control_hashes(root: Path) -> dict[str, str]:
    public_paths = privacy_gate.paths_from_manifest(root, root / "integration/nightly/public-files.txt")
    workflow_document = _load_document(root / WORKFLOW_MANIFEST_PATH)
    rows = workflow_document.get("workflows")
    if not isinstance(rows, list):
        raise NightlyError("workflow manifest is missing its workflow list")
    workflow_paths: list[Path] = []
    for row in rows:
        workflow = row.get("workflow") if isinstance(row, dict) else None
        if not isinstance(workflow, str) or re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow) is None:
            raise NightlyError("workflow manifest contains an invalid workflow")
        workflow_paths.append(Path(".github/workflows") / workflow)
    relative_paths = {
        *public_paths,
        *workflow_paths,
        Path(".gitmodules"),
        Path("scripts/install-zig-ci.sh"),
    }
    hashes: dict[str, str] = {}
    for relative in sorted(relative_paths):
        path = root / relative
        if not path.is_file():
            raise NightlyError(f"trusted control file is missing: {relative.as_posix()}")
        hashes[relative.as_posix()] = _digest_path(path)
    hashes.update(optional_control_pipeline_hashes(root))
    repositories = (("@control", root), ("@candidate", _candidate_root(root)))
    for prefix, repository in repositories:
        for git_path_name in ("config", "config.worktree", "hooks"):
            raw = _git(repository, ["rev-parse", "--git-path", git_path_name], f"trusted {git_path_name} path")
            git_path = Path(raw)
            if not git_path.is_absolute():
                git_path = repository / git_path
            hashes[f"{prefix}-{git_path_name}"] = _digest_path(git_path)
    return hashes


def _validated_control_hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise NightlyError("nightly pair state is missing trusted control hashes")
    hashes: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not key or not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise NightlyError("nightly pair state has invalid trusted control hashes")
        hashes[key] = digest
    return hashes


def load_public_repository(root: Path) -> str:
    document = _load_document(root / WORKFLOW_MANIFEST_PATH)
    repository = document.get("repository")
    if not isinstance(repository, str):
        raise NightlyError("workflow manifest is missing its public repository")
    return _validate_repository(repository)


def write_pair_state(
    path: Path,
    baseline_sha: str,
    candidate_sha: str,
    lease_sha: str,
    *,
    control_sha: str,
    control_hashes: Mapping[str, str],
) -> None:
    _write_document(
        path,
        {
            "schema": _PAIR_SCHEMA,
            "branch": INTEGRATION_BRANCH,
            "baseline_sha": _validate_sha(baseline_sha, "baseline_sha"),
            "candidate_sha": _validate_sha(candidate_sha, "candidate_sha"),
            "lease_sha": _validate_sha(lease_sha, "lease_sha"),
            "control_sha": _validate_sha(control_sha, "control_sha"),
            "control_hashes": _validated_control_hashes(dict(control_hashes)),
        },
    )


def load_pair_state(path: Path) -> dict[str, Any]:
    document = _load_document(path)
    if document.get("schema") != _PAIR_SCHEMA or document.get("branch") != INTEGRATION_BRANCH:
        raise NightlyError("nightly pair state has the wrong schema or branch")
    for key in ("baseline_sha", "candidate_sha", "lease_sha", "control_sha"):
        document[key] = _validate_sha(str(document.get(key, "")), key)
    document["control_hashes"] = _validated_control_hashes(document.get("control_hashes"))
    for key in (
        "validated_rust_sha",
        "validated_node_sha",
        "validated_python_sha",
        "candidate_published_sha",
        "report_sha",
        "report_published_sha",
    ):
        if key in document:
            document[key] = _validate_sha(str(document[key]), key)
    return document


def require_pair(
    pair: Mapping[str, Any],
    *,
    baseline_sha: str | None = None,
    candidate_sha: str | None = None,
    report_sha: str | None = None,
) -> None:
    expected = {
        "baseline_sha": baseline_sha,
        "candidate_sha": candidate_sha,
        "report_sha": report_sha,
    }
    for key, value in expected.items():
        if value is not None and pair.get(key) != _validate_sha(value, key):
            raise NightlyError(f"nightly pair state {key} does not match")

def require_trusted_attestation(
    pair: Mapping[str, Any],
    *,
    control_sha: str,
    head_sha: str,
    head_key: str,
    control_hashes: Mapping[str, str],
    required_markers: Sequence[str] = (),
) -> None:
    if head_key not in {"candidate_sha", "report_sha"}:
        raise NightlyError("trusted attestation head key is invalid")
    expected_control = _validate_sha(str(pair.get("control_sha", "")), "control_sha")
    current_control = _validate_sha(control_sha, "control_sha")
    if current_control != expected_control:
        raise NightlyError("trusted control checkout moved")
    expected_head = _validate_sha(str(pair.get(head_key, "")), head_key)
    if _validate_sha(head_sha, head_key) != expected_head:
        raise NightlyError("trusted candidate checkout moved")
    if _validated_control_hashes(pair.get("control_hashes")) != _validated_control_hashes(dict(control_hashes)):
        raise NightlyError("trusted control files changed")
    for marker in required_markers:
        if pair.get(marker) != expected_head:
            raise NightlyError(f"nightly pair state is missing {marker}")


def attest_trusted_state(
    root: Path,
    pair_path: Path,
    *,
    head_key: str,
    required_markers: Sequence[str] = (),
) -> dict[str, Any]:
    if pair_path.resolve() != (root / PAIR_STATE_PATH).resolve():
        raise NightlyError("nightly pair state path is outside trusted control")
    pair = load_pair_state(pair_path)
    control_sha = _isolated_git(root, ["rev-parse", "HEAD"], "trusted control revision")
    candidate = _candidate_root(root)
    head_sha = _isolated_git(candidate, ["rev-parse", "HEAD"], "trusted candidate revision")
    if _isolated_git(candidate, ["status", "--porcelain"], "trusted candidate cleanliness"):
        raise NightlyError("trusted candidate checkout is not clean")
    require_trusted_attestation(
        pair,
        control_sha=control_sha,
        head_sha=head_sha,
        head_key=head_key,
        control_hashes=trusted_control_hashes(root),
        required_markers=required_markers,
    )
    mutable = {REPORT_PATH.as_posix(), STATE_PATH.as_posix()} if head_key == "report_sha" else set()
    for relative, expected_digest in pair["control_hashes"].items():
        if relative.startswith("@") or relative in mutable:
            continue
        candidate_control = candidate / relative
        if not candidate_control.is_file() or _digest_path(candidate_control) != expected_digest:
            raise NightlyError(f"candidate changed trusted control file: {relative}")
    return pair


def _update_pair(path: Path, pair: Mapping[str, Any], **updates: str) -> dict[str, Any]:
    document = dict(pair)
    for key, value in updates.items():
        document[key] = _validate_sha(value, key)
    _write_document(path, document)
    return document


def _candidate_root(root: Path) -> Path:
    return root / CANDIDATE_PATH


def _state_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _verify_candidate(root: Path, pair: Mapping[str, Any]) -> Path:
    candidate = _candidate_root(root)
    if not candidate.is_dir():
        raise NightlyError("shared candidate checkout is missing")
    head = _validate_sha(_isolated_git(candidate, ["rev-parse", "HEAD"], "candidate revision"), "candidate_sha")
    require_pair(pair, candidate_sha=head)
    if _isolated_git(candidate, ["status", "--porcelain"], "candidate cleanliness"):
        raise NightlyError("shared candidate checkout is not clean")
    return candidate


def _is_nightly_automation_path(path: Path) -> bool:
    if (
        path in (Path(".woodpecker.yml"), Path(".woodpecker-control.yml"))
        or path.parts[:1] == (".github",)
    ):
        return True
    if path.parts[:2] == ("integration", "nightly"):
        return True
    if path.parts[:2] == ("scripts", "integration"):
        return True
    if path.parts[:1] == ("scripts",) and path.name.startswith("perf"):
        return True
    if path.parts[:1] == ("tests",) and path.name.startswith(
        ("test_integration_nightly_", "test_nightly_ci_", "test_perf_")
    ):
        return True
    return False


def reviewable_changed_paths(root: Path, changed_paths: Iterable[Path]) -> list[Path]:
    return sorted({path for path in changed_paths if (root / path).is_file()})


def require_product_manifest_coverage(
    changed_paths: Iterable[Path],
    manifested_paths: Iterable[Path],
) -> None:
    expected = {path for path in changed_paths if not _is_nightly_automation_path(path)}
    manifested = set(manifested_paths)
    missing = sorted(expected - manifested)
    if missing:
        raise NightlyError(f"reviewed product manifest is missing: {missing[0].as_posix()}")
    extra = sorted(manifested - expected)
    if extra:
        raise NightlyError(f"reviewed product manifest has a stale path: {extra[0].as_posix()}")


def select_candidate_seed(
    source_sha: str,
    lease_sha: str,
    bootstrap_source_sha: str = "",
    bootstrap_lease_sha: str = "",
) -> str:
    lease = _validate_sha(lease_sha, "lease_sha")
    if source_sha.strip():
        return _validate_sha(source_sha, "source_sha")
    has_bootstrap_source = bool(bootstrap_source_sha.strip())
    has_bootstrap_lease = bool(bootstrap_lease_sha.strip())
    if has_bootstrap_source != has_bootstrap_lease:
        raise NightlyError("bootstrap candidate seed is incomplete")
    if not has_bootstrap_source:
        return lease
    bootstrap_source = _validate_sha(bootstrap_source_sha, "bootstrap_source_sha")
    bootstrap_lease = _validate_sha(bootstrap_lease_sha, "bootstrap_lease_sha")
    return bootstrap_source if lease == bootstrap_lease else lease


def prepare_shared_candidate(
    root: Path,
    source_sha: str,
    branch: str,
    pair_path: Path,
    bootstrap_source_sha: str = "",
    bootstrap_lease_sha: str = "",
) -> dict[str, Any]:
    control = _validate_sha(_isolated_git(root, ["rev-parse", "HEAD"], "trusted control revision"), "control_sha")
    if branch != INTEGRATION_BRANCH:
        raise NightlyError(f"branch must be exactly {INTEGRATION_BRANCH}")
    public_repository = load_public_repository(root)
    public_remote = f"https://github.com/{public_repository}.git"
    remote_head = _isolated_git(
        root,
        ["ls-remote", "--heads", public_remote, f"refs/heads/{branch}"],
        "public integration branch lease lookup",
    ).split()
    if len(remote_head) != 2 or remote_head[1] != f"refs/heads/{branch}":
        raise NightlyError("public integration branch must already exist")
    lease = _validate_sha(remote_head[0], "lease_sha")
    source = select_candidate_seed(source_sha, lease, bootstrap_source_sha, bootstrap_lease_sha)

    candidate = _candidate_root(root)
    if candidate.exists():
        raise NightlyError("shared candidate checkout already exists")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    create_isolated_candidate(
        root,
        candidate,
        source,
        public_remote=public_remote,
        branch=branch,
        expected_lease=lease,
    )
    _isolated_git(
        candidate,
        ["fetch", "--quiet", "--no-tags", PUBLIC_UPSTREAM, "+main:refs/nightly/upstream-main"],
        "upstream fetch",
    )
    baseline = _validate_sha(
        _isolated_git(candidate, ["rev-parse", "refs/nightly/upstream-main"], "upstream revision"),
        "baseline_sha",
    )
    merge_base = _validate_sha(
        _isolated_git(candidate, ["merge-base", baseline, source], "candidate merge base"),
        "merge_base",
    )
    candidate_sha = rebase_candidate(candidate, baseline, merge_base, source)
    manifested_products = privacy_gate.paths_from_manifest(root, root / PRODUCT_MANIFEST_PATH)
    require_product_manifest_coverage(
        reviewable_changed_paths(
            candidate,
            privacy_gate.changed_paths(candidate, baseline, candidate_sha),
        ),
        manifested_products,
    )
    write_pair_state(
        pair_path,
        baseline,
        candidate_sha,
        lease,
        control_sha=control,
        control_hashes=trusted_control_hashes(root),
    )
    return load_pair_state(pair_path)


def _safe_agent_environment(root: Path) -> tuple[dict[str, str], Path]:
    del root
    provider_key = os.environ.get("PI_GPT_PROXY_API_KEY", "").strip()
    base_url, _ = _validate_http_url(os.environ.get("GPT_PROXY_BASE_URL", ""), "GPT proxy base URL")
    if not provider_key:
        raise NightlyError("GPT proxy API key is missing")

    temporary_home = Path(tempfile.mkdtemp(prefix="cmux-omp-home-", dir="/tmp"))
    config_dir_name = ".omp-nightly-config"
    agent_dir = temporary_home / "coding-agent"
    agent_dir.mkdir(mode=0o700)
    models = {
        "providers": {
            "gpt-proxy": {
                "baseUrl": base_url,
                "api": "openai-responses",
                "authHeader": True,
                "apiKey": "PI_GPT_PROXY_API_KEY",
                "compat": {"supportsDeveloperRole": True},
                "models": [
                    {
                        "id": "gpt-5.6-sol",
                        "name": "GPT 5.6 Sol Proxy",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 372000,
                        "maxTokens": 128000,
                        "preferWebsockets": False,
                        "thinking": {"mode": "effort", "minLevel": "low", "maxLevel": "xhigh"},
                    }
                ],
            }
        }
    }
    config = {"modelRoles": {"default": "gpt-proxy/gpt-5.6-sol"}}
    for path, document in ((agent_dir / "models.yml", models), (agent_dir / "config.yml", config)):
        path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)

    environment = {
        "HOME": str(temporary_home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "NO_COLOR": "1",
        "PI_CONFIG_DIR": config_dir_name,
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_GPT_PROXY_API_KEY": provider_key,
    }
    for name in ("LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment, temporary_home


def _kill_uid_processes(uid: int) -> None:
    for _ in range(10):
        found = False
        for status_path in Path("/proc").glob("[0-9]*/status"):
            try:
                lines = status_path.read_text(encoding="utf-8").splitlines()
                uid_line = next(line for line in lines if line.startswith("Uid:"))
                process_uids = {int(value) for value in uid_line.split()[1:]}
                pid = int(status_path.parent.name)
            except (OSError, StopIteration, ValueError):
                continue
            if uid not in process_uids or pid == os.getpid():
                continue
            found = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not found:
            return
        time.sleep(0.01)
    raise NightlyError("untrusted candidate processes survived isolation cleanup")


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    os.chown(root, uid, gid, follow_symlinks=False)
    for path in root.rglob("*"):
        os.chown(path, uid, gid, follow_symlinks=False)


def _inventory(root: Path) -> dict[Path, tuple[str, str, int]]:
    inventory: dict[Path, tuple[str, str, int]] = {}
    if not root.exists():
        return inventory
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            inventory[relative] = ("symlink", os.readlink(path), 0)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            inventory[relative] = ("file", digest, stat.S_IMODE(path.stat().st_mode))
    return inventory


def sync_reviewed_product_changes(candidate: Path, sandbox: Path, reviewed_paths: Iterable[Path]) -> list[Path]:
    approved = set(reviewed_paths)
    before = _inventory(candidate)
    after = _inventory(sandbox)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    rejected = [path for path in changed if path not in approved]
    if rejected:
        raise NightlyError(f"OMP changed an unreviewed path: {rejected[0].as_posix()}")
    for relative in changed:
        source = sandbox / relative
        target = candidate / relative
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if not source.exists() and not source.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
    return changed


def _changed_paths_from_porcelain(status: str) -> set[Path]:
    changed: set[Path] = set()
    for entry in status.split("\0"):
        if len(entry) <= 3:
            continue
        relative = Path(entry[3:])
        if relative.parts[:1] == ("ghostty",):
            continue
        changed.add(relative)
    return changed


def reconcile_candidate(
    root: Path,
    pair_path: Path,
    omp_command: Sequence[str],
    agent_uid: int,
) -> str:
    if os.geteuid() != 0:
        raise NightlyError("OMP reconciliation requires a root container boundary")
    if agent_uid <= 0:
        raise NightlyError("OMP agent UID must be unprivileged")
    pair = load_pair_state(pair_path)
    candidate = _verify_candidate(root, pair)
    reviewed = privacy_gate.paths_from_manifest(root, root / PRODUCT_MANIFEST_PATH)
    if not reviewed:
        return pair["candidate_sha"]
    if not omp_command:
        raise NightlyError("OMP command is required")

    sandbox = Path(tempfile.mkdtemp(prefix="cmux-omp-public-", dir="/tmp"))
    temporary_home: Path | None = None
    root_stat = root.stat()
    try:
        prefix = str(sandbox) + os.sep
        _isolated_git(candidate, ["checkout-index", "--all", f"--prefix={prefix}"], "public sandbox export")
        environment, temporary_home = _safe_agent_environment(root)
        _chown_tree(sandbox, agent_uid, agent_uid)
        _chown_tree(temporary_home, agent_uid, agent_uid)
        os.chown(root, 0, 0)
        os.chmod(root, 0o700)
        _run(
            omp_command,
            cwd=sandbox,
            env=environment,
            label="restricted OMP reconciliation",
            uid=agent_uid,
            gid=agent_uid,
        )
        _kill_uid_processes(agent_uid)
        os.chmod(root, stat.S_IMODE(root_stat.st_mode))
        os.chown(root, root_stat.st_uid, root_stat.st_gid)
        sync_reviewed_product_changes(candidate, sandbox, reviewed)
    finally:
        try:
            _kill_uid_processes(agent_uid)
        finally:
            try:
                os.chmod(root, stat.S_IMODE(root_stat.st_mode))
                os.chown(root, root_stat.st_uid, root_stat.st_gid)
            finally:
                if temporary_home is not None:
                    shutil.rmtree(temporary_home, ignore_errors=True)
                shutil.rmtree(sandbox, ignore_errors=True)

    status = _isolated_git(
        candidate,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
        "candidate status",
        strip_output=False,
    )
    if status:
        changed_paths = _changed_paths_from_porcelain(status)
        unreviewed = sorted(changed_paths - set(reviewed))
        if unreviewed:
            raise NightlyError(
                f"candidate contains changes outside reviewed product paths: {unreviewed[0].as_posix()}"
            )
        _isolated_git(
            candidate,
            ["add", "--all", "--", *[path.as_posix() for path in sorted(changed_paths)]],
            "candidate staging",
        )
        commit_env = isolated_git_environment()
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": _PUBLIC_GIT_NAME,
                "GIT_AUTHOR_EMAIL": _PUBLIC_GIT_EMAIL,
                "GIT_COMMITTER_NAME": _PUBLIC_GIT_NAME,
                "GIT_COMMITTER_EMAIL": _PUBLIC_GIT_EMAIL,
            }
        )
        _git(
            candidate,
            [
                *_ISOLATED_GIT_CONFIG_ARGS,
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                "chore(integration): reconcile nightly candidate",
            ],
            "candidate reconciliation commit",
            env=commit_env,
        )
    candidate_sha = _validate_sha(
        _isolated_git(candidate, ["rev-parse", "HEAD"], "reconciled candidate revision"),
        "candidate_sha",
    )
    _update_pair(pair_path, pair, candidate_sha=candidate_sha)
    return candidate_sha


def privacy_candidate(root: Path, pair_path: Path) -> None:
    pair = attest_trusted_state(root, pair_path, head_key="candidate_sha")
    candidate = _verify_candidate(root, pair)
    denylist = privacy_gate.load_runtime_denylist(required=True)
    findings = privacy_gate.scan_diff(candidate, pair["baseline_sha"], pair["candidate_sha"], denylist=denylist)
    findings.extend(
        scan_candidate_commit_metadata(
            candidate,
            pair["baseline_sha"],
            pair["candidate_sha"],
            denylist=denylist,
        )
    )
    if findings:
        raise NightlyError("candidate failed the public privacy gate")


def _validation_environment(home: Path) -> dict[str, str]:
    cache = home / ".cache"
    cargo_home = home / ".cargo"
    npm_cache = cache / "npm"
    for path in (cache, cargo_home, npm_cache):
        path.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CARGO_HOME": str(cargo_home),
        "NPM_CONFIG_CACHE": str(npm_cache),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in (
        "CARGO_HOME",
        "CARGO_TARGET_DIR",
        "CARGO_INCREMENTAL",
        "CARGO_PROFILE_DEV_DEBUG",
        "CARGO_PROFILE_TEST_DEBUG",
        "NPM_CONFIG_CACHE",
        "RUSTUP_HOME",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    for name in ("LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def run_isolated_commands(
    root: Path,
    sandbox: Path,
    home: Path,
    commands: Sequence[tuple[Sequence[str], Path]],
    *,
    agent_uid: int,
    environment_overrides: Mapping[str, str] | None = None,
    chown_sandbox: bool = True,
    remove_sandbox: bool = True,
) -> None:
    if os.geteuid() != 0:
        raise NightlyError("candidate validation requires a root container boundary")
    if agent_uid <= 0:
        raise NightlyError("candidate validation UID must be unprivileged")
    trusted_root = root.resolve()
    sandbox_root = sandbox.resolve()
    home_root = home.resolve()
    if sandbox_root.is_relative_to(trusted_root) or home_root.is_relative_to(trusted_root):
        raise NightlyError("candidate validation sandbox must be outside trusted control")
    if not sandbox_root.is_dir() or not home_root.is_dir():
        raise NightlyError("candidate validation sandbox is missing")
    environment = _validation_environment(home_root)
    environment.update(environment_overrides or {})
    if chown_sandbox:
        _chown_tree(sandbox_root, agent_uid, agent_uid)
    _chown_tree(home_root, agent_uid, agent_uid)
    root_stat = trusted_root.stat()
    try:
        os.chown(trusted_root, 0, 0)
        os.chmod(trusted_root, 0o700)
        for command, relative_cwd in commands:
            cwd = (sandbox_root / relative_cwd).resolve()
            if not cwd.is_relative_to(sandbox_root) or not cwd.is_dir():
                raise NightlyError("candidate validation working directory escaped its sandbox")
            _run(
                command,
                cwd=cwd,
                env=environment,
                label="unprivileged candidate validation",
                uid=agent_uid,
                gid=agent_uid,
            )
    finally:
        try:
            _kill_uid_processes(agent_uid)
        finally:
            try:
                os.chmod(trusted_root, stat.S_IMODE(root_stat.st_mode))
                os.chown(trusted_root, root_stat.st_uid, root_stat.st_gid)
            finally:
                if remove_sandbox:
                    shutil.rmtree(sandbox_root, ignore_errors=True)
                shutil.rmtree(home_root, ignore_errors=True)


def _export_ghostty(candidate: Path, sandbox: Path) -> None:
    tree_entry = _isolated_git(candidate, ["ls-tree", "HEAD", "ghostty"], "ghostty gitlink lookup").split()
    if len(tree_entry) < 3 or tree_entry[1] != "commit":
        raise NightlyError("candidate ghostty gitlink is missing")
    expected = _validate_sha(tree_entry[2], "ghostty gitlink")
    repository = Path(tempfile.mkdtemp(prefix="cmux-ghostty-export-", dir="/tmp"))
    home = Path(tempfile.mkdtemp(prefix="cmux-ghostty-home-", dir="/tmp"))
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        _run(["git", "init", "--quiet"], cwd=repository, env=environment, label="ghostty export initialization")
        _run(
            ["git", "-c", "core.hooksPath=/dev/null", "fetch", "--quiet", "--depth", "1", _GHOSTTY_UPSTREAM, expected],
            cwd=repository,
            env=environment,
            label="ghostty exact revision fetch",
        )
        actual = _validate_sha(_run(["git", "rev-parse", "FETCH_HEAD"], cwd=repository, env=environment), "ghostty revision")
        if actual != expected:
            raise NightlyError("ghostty export does not match its gitlink")
        _run(["git", "read-tree", actual], cwd=repository, env=environment, label="ghostty export index")
        destination = sandbox / "ghostty"
        destination.mkdir()
        _run(
            ["git", "checkout-index", "--all", f"--prefix={str(destination) + os.sep}"],
            cwd=repository,
            env=environment,
            label="ghostty source export",
        )
    finally:
        shutil.rmtree(repository, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def _validate_candidate_in_sandbox(
    root: Path,
    pair_path: Path,
    commands: Sequence[tuple[Sequence[str], Path]],
    *,
    agent_uid: int,
    marker: str,
    include_ghostty: bool = False,
) -> None:
    pair = attest_trusted_state(root, pair_path, head_key="candidate_sha")
    candidate = _verify_candidate(root, pair)
    sandbox = Path(tempfile.mkdtemp(prefix="cmux-candidate-validation-", dir="/tmp"))
    home = Path(tempfile.mkdtemp(prefix="cmux-candidate-home-", dir="/tmp"))
    try:
        _git(
            candidate,
            ["checkout-index", "--all", f"--prefix={str(sandbox) + os.sep}"],
            "exact candidate validation export",
        )
        if _inventory(candidate) != _inventory(sandbox):
            raise NightlyError("candidate validation export is not exact")
        if include_ghostty:
            _export_ghostty(candidate, sandbox)
        run_isolated_commands(
            root,
            sandbox,
            home,
            commands,
            agent_uid=agent_uid,
            environment_overrides={"CMUX_TUI_BUILD_COMMIT": pair["candidate_sha"]},
        )
    except BaseException:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
        raise
    pair = attest_trusted_state(root, pair_path, head_key="candidate_sha")
    _update_pair(pair_path, pair, **{marker: pair["candidate_sha"]})


# Rust validation pods are capped at four CPUs. Match the test harness fan-out
# to that cgroup so process-heavy SSH and terminal-host tests do not starve.
def validate_source_rust(root: Path, agent_uid: int) -> None:
    sandbox = Path("/opt/rmux-source")
    marker = Path("/opt/rmux-source-tree")
    expected = _validate_sha(
        _git(root, ["rev-parse", "HEAD:cmux-tui"], "source tree revision"),
        "source tree revision",
    )
    if not marker.is_file() or _validate_sha(
        marker.read_text(encoding="utf-8").strip(),
        "cached source tree revision",
    ) != expected:
        raise NightlyError("cached Rust source does not match the checked-out source tree")
    home = Path(tempfile.mkdtemp(prefix="cmux-source-home-", dir="/tmp"))
    run_isolated_commands(
        root,
        sandbox,
        home,
        [(["cargo", "test", "--workspace", "--locked", "--", "--test-threads=4"], Path("cmux-tui"))],
        agent_uid=agent_uid,
        environment_overrides={
            "CMUX_GHOSTTY_SRC": "/opt/ghostty",
            "CMUX_TUI_BUILD_COMMIT": expected,
        },
        chown_sandbox=False,
        remove_sandbox=False,
    )


def validate_candidate_rust(root: Path, pair_path: Path, agent_uid: int) -> None:
    _validate_candidate_in_sandbox(
        root,
        pair_path,
        [(["cargo", "test", "--workspace", "--locked", "--", "--test-threads=4"], Path("cmux-tui"))],
        agent_uid=agent_uid,
        marker="validated_rust_sha",
        include_ghostty=True,
    )


def validate_candidate_node(root: Path, pair_path: Path, agent_uid: int) -> None:
    binding = Path("cmux-tui/bindings/typescript")
    _validate_candidate_in_sandbox(
        root,
        pair_path,
        [
            (["npm", "ci", "--no-audit", "--no-fund"], binding),
            (["npm", "test"], binding),
        ],
        agent_uid=agent_uid,
        marker="validated_node_sha",
    )


def validate_candidate_python(root: Path, pair_path: Path, agent_uid: int) -> None:
    commands: list[tuple[Sequence[str], Path]] = [
        (
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "cmux-tui/bindings/python/tests",
                "-t",
                "cmux-tui/bindings/python",
            ),
            Path("."),
        ),
        ((sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_perf_*.py"), Path(".")),
        ((sys.executable, "tests/test_integration_nightly_automation.py"), Path(".")),
        ((sys.executable, "tests/test_nightly_ci_hardening.py"), Path(".")),
    ]
    _validate_candidate_in_sandbox(
        root,
        pair_path,
        commands,
        agent_uid=agent_uid,
        marker="validated_python_sha",
    )


def _push_environment(token: str) -> dict[str, str]:
    value = token.strip()
    if not value:
        raise NightlyError("fork push credential is missing")
    credential = base64.b64encode(f"x-access-token:{value}".encode("utf-8")).decode("ascii")
    environment = isolated_git_environment()
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
        }
    )
    return environment


def _public_remote_head(root: Path, repository: str) -> str:
    output = _isolated_git(
        root,
        ["ls-remote", "--heads", f"https://github.com/{repository}.git", f"refs/heads/{INTEGRATION_BRANCH}"],
        "public integration branch lookup",
    ).split()
    if len(output) != 2 or output[1] != f"refs/heads/{INTEGRATION_BRANCH}":
        raise NightlyError("public integration branch lookup is invalid")
    return _validate_sha(output[0], "remote branch SHA")


def push_candidate(root: Path, pair_path: Path) -> str:
    pair = attest_trusted_state(
        root,
        pair_path,
        head_key="candidate_sha",
        required_markers=("validated_rust_sha", "validated_node_sha", "validated_python_sha"),
    )
    candidate = _verify_candidate(root, pair)
    repository = load_public_repository(root)
    remote_head = _public_remote_head(root, repository)
    if remote_head == pair["candidate_sha"]:
        _update_pair(pair_path, pair, candidate_published_sha=remote_head)
        return remote_head
    if remote_head != pair["lease_sha"]:
        raise NightlyError("public integration branch moved before candidate push")
    environment = _push_environment(os.environ.get("CMUX_FORK_PUSH_TOKEN", ""))
    _git(
        candidate,
        [
            *_ISOLATED_GIT_CONFIG_ARGS,
            "push",
            "--no-verify",
            "--quiet",
            f"--force-with-lease=refs/heads/{INTEGRATION_BRANCH}:{pair['lease_sha']}",
            f"https://github.com/{repository}.git",
            f"{pair['candidate_sha']}:refs/heads/{INTEGRATION_BRANCH}",
        ],
        "candidate publication",
        env=environment,
    )
    _update_pair(pair_path, pair, candidate_published_sha=pair["candidate_sha"])
    return pair["candidate_sha"]


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _render_report(summary: Mapping[str, Any], commits: Sequence[tuple[str, str]], manifest: Mapping[str, Any]) -> str:
    lines = [
        "# Nightly integration report",
        "",
        "This is the single updating public report for the aggregate integration branch.",
        "The pull request remains static; automation does not create, edit, or close pull requests.",
        "The fork commit status and aggregate pull request Checks pages are public.",
        "Woodpecker UI and logs remain private and are not required to review the public result.",
        "",
        f"- Upstream baseline: `{summary.get('baseline_sha', 'unavailable')}`",
        f"- Integration candidate: `{summary.get('candidate_sha', 'unavailable')}`",
        f"- Benchmark status: `{summary.get('status', 'unavailable')}`",
        "",
        "## Aggregate benchmark deltas",
        "",
        "| Metric | Candidate | vs upstream | vs prior nightly |",
        "| --- | ---: | ---: | ---: |",
    ]
    metrics = summary.get("metrics", {})
    baseline_deltas = summary.get("deltas_from_baseline", {})
    previous_deltas = summary.get("deltas_from_previous", {})
    if isinstance(metrics, dict):
        for metric in sorted(metrics):
            upstream = baseline_deltas.get(metric, "n/a") if isinstance(baseline_deltas, dict) else "n/a"
            previous = previous_deltas.get(metric, "n/a") if isinstance(previous_deltas, dict) else "n/a"
            lines.append(f"| `{metric}` | {_format_value(metrics[metric])} | {_format_value(upstream)} | {_format_value(previous)} |")
    lines.extend(("", "## Commits since prior nightly", ""))
    if commits:
        lines.extend(f"- `{short_sha}` {subject}" for short_sha, subject in commits)
    else:
        lines.append("- No new integration commits.")
    lines.extend(("", "## Canonical pull request manifest", ""))
    rows = manifest.get("pull_requests", [])
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                lines.append(
                    f"- [#{row.get('number')}](https://github.com/manaflow-ai/cmux/pull/{row.get('number')}) "
                    f"`{row.get('classification')}` by `@{row.get('author')}`"
                )
    lines.append("")
    return "\n".join(lines)


def update_public_report(
    root: Path,
    summary: Mapping[str, Any],
    *,
    commits: Sequence[tuple[str, str]],
    manifest: Mapping[str, Any],
    commit_records: Sequence[Mapping[str, str]] = (),
) -> tuple[Path, Path]:
    report = root / REPORT_PATH
    state = root / STATE_PATH
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_render_report(summary, commits, manifest), encoding="utf-8")
    state_document = dict(summary)
    state_document["integration_commits"] = [dict(row) for row in commit_records]
    benchmark_contract.write_json(state, state_document)
    return report, state


def _commit_records(root: Path, baseline_sha: str, candidate_sha: str) -> list[dict[str, str]]:
    output = _git(
        root,
        ["log", "--reverse", "--no-merges", "--format=%H%x09%s", f"{baseline_sha}..{candidate_sha}", "--"],
        "integration commit list",
    )
    records: list[dict[str, str]] = []
    for line in output.splitlines():
        sha, separator, subject = line.partition("\t")
        if not separator:
            raise NightlyError("integration commit list is malformed")
        changed = _git(root, ["diff-tree", "--no-commit-id", "--name-only", "-r", sha, "--"], "commit path list")
        paths = {Path(item) for item in changed.splitlines() if item}
        if paths and paths <= {REPORT_PATH, STATE_PATH}:
            continue
        patch = _run(["git", "show", "--pretty=format:", "--binary", sha, "--"], cwd=root, label="commit patch calculation")
        patch_id_output = _run(["git", "patch-id", "--stable"], cwd=root, input_text=patch, label="commit patch identity")
        patch_id = patch_id_output.split()[0] if patch_id_output else sha
        records.append({"patch_id": patch_id, "sha": sha, "subject": subject})
    return records


def render_report_commit(root: Path, pair_path: Path, benchmark_summary_path: Path) -> str:
    pair = attest_trusted_state(
        root,
        pair_path,
        head_key="candidate_sha",
        required_markers=(
            "validated_rust_sha",
            "validated_node_sha",
            "validated_python_sha",
            "candidate_published_sha",
        ),
    )
    candidate = _verify_candidate(root, pair)
    summary = _load_document(benchmark_summary_path)
    require_pair(
        pair,
        baseline_sha=str(summary.get("baseline_sha", "")),
        candidate_sha=str(summary.get("candidate_sha", "")),
    )
    if summary.get("accepted") is not True or summary.get("status") != "accepted":
        raise NightlyError("benchmark summary was not accepted")
    previous = _load_document(candidate / STATE_PATH)
    compared = benchmark_contract.compare_with_previous(previous, summary)
    manifest = _load_document(candidate / MANIFEST_PATH)
    records = _commit_records(candidate, pair["baseline_sha"], pair["candidate_sha"])
    previous_ids = {
        row.get("patch_id")
        for row in previous.get("integration_commits", [])
        if isinstance(row, dict) and isinstance(row.get("patch_id"), str)
    }
    new_records = [row for row in records if row["patch_id"] not in previous_ids]
    update_public_report(
        candidate,
        compared,
        commits=[(row["sha"][:12], row["subject"]) for row in new_records],
        manifest=manifest,
        commit_records=records,
    )
    denylist = privacy_gate.load_runtime_denylist(required=True)
    findings = privacy_gate.scan_paths(candidate, [REPORT_PATH, STATE_PATH], denylist=denylist)
    if findings:
        raise NightlyError("public nightly report failed the privacy gate")
    _isolated_git(
        candidate,
        ["add", REPORT_PATH.as_posix(), STATE_PATH.as_posix()],
        "nightly report staging",
    )
    commit_env = isolated_git_environment()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": _PUBLIC_GIT_NAME,
            "GIT_AUTHOR_EMAIL": _PUBLIC_GIT_EMAIL,
            "GIT_COMMITTER_NAME": _PUBLIC_GIT_NAME,
            "GIT_COMMITTER_EMAIL": _PUBLIC_GIT_EMAIL,
        }
    )
    _git(
        candidate,
        [
            *_ISOLATED_GIT_CONFIG_ARGS,
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            "chore(integration): update nightly report",
        ],
        "nightly report commit",
        env=commit_env,
    )
    report_sha = _validate_sha(
        _isolated_git(candidate, ["rev-parse", "HEAD"], "nightly report revision"),
        "report_sha",
    )
    if scan_candidate_commit_metadata(
        candidate,
        pair["baseline_sha"],
        report_sha,
        denylist=denylist,
    ):
        raise NightlyError("nightly report commit failed the public privacy gate")
    _update_pair(pair_path, pair, report_sha=report_sha)
    return report_sha


def push_report(root: Path, pair_path: Path) -> str:
    pair = attest_trusted_state(root, pair_path, head_key="report_sha")
    report_sha = _validate_sha(str(pair.get("report_sha", "")), "report_sha")
    candidate = _candidate_root(root)
    repository = load_public_repository(root)
    remote_head = _public_remote_head(root, repository)
    if remote_head == report_sha:
        _update_pair(pair_path, pair, report_published_sha=report_sha)
        return report_sha
    if remote_head != pair["candidate_sha"]:
        raise NightlyError("public integration branch moved before report push")
    environment = _push_environment(os.environ.get("CMUX_FORK_PUSH_TOKEN", ""))
    _git(
        candidate,
        [
            *_ISOLATED_GIT_CONFIG_ARGS,
            "push",
            "--no-verify",
            "--quiet",
            f"--force-with-lease=refs/heads/{INTEGRATION_BRANCH}:{pair['candidate_sha']}",
            f"https://github.com/{repository}.git",
            f"{report_sha}:refs/heads/{INTEGRATION_BRANCH}",
        ],
        "nightly report publication",
        env=environment,
    )
    _update_pair(pair_path, pair, report_published_sha=report_sha)
    return report_sha


def publish_github_status(
    repository: str,
    report_sha: str,
    credential_value: str,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> str:
    repo = _validate_repository(repository)
    report = _validate_sha(report_sha, "report_sha")
    credential = credential_value.strip()
    if not credential:
        raise NightlyError("GitHub status token is missing")
    if attempts < 1 or attempts > 6:
        raise NightlyError("GitHub status attempts are outside the bounded policy")
    payload = json.dumps(
        {
            "state": "success",
            "context": "Woodpecker CI / nightly report",
            "description": "Nightly integration report verified",
            "target_url": f"https://github.com/{repo}/commit/{report}/checks",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/statuses/{report}",
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "User-Agent": "cmux-nightly-integration",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", 0))
                body = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            body = b""
        except (OSError, urllib.error.URLError) as error:
            if attempt == attempts:
                raise NightlyError("GitHub report status request failed") from error
            sleep(float(2 ** (attempt - 1)))
            continue
        if status == 429 or status >= 500:
            if attempt == attempts:
                raise NightlyError("GitHub report status request exhausted retries")
            sleep(float(2 ** (attempt - 1)))
            continue
        if status < 200 or status >= 300:
            raise NightlyError("GitHub report status request was rejected")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise NightlyError("GitHub report status response is invalid") from error
        response_sha = _validate_sha(str(document.get("sha", "")), "status response SHA")
        if response_sha != report:
            raise NightlyError("GitHub status response does not match the report commit")
        return response_sha
    raise NightlyError("GitHub report status request failed")


def publish_report_status(root: Path, pair_path: Path) -> str:
    pair = attest_trusted_state(
        root,
        pair_path,
        head_key="report_sha",
        required_markers=("report_published_sha",),
    )
    report_sha = _validate_sha(str(pair.get("report_sha", "")), "report_sha")
    repository = load_public_repository(root)
    return publish_github_status(repository, report_sha, os.environ.get("GITHUB_TOKEN", ""))


def _add_common_state_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--pair-state", type=Path, default=PAIR_STATE_PATH)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    _add_common_state_argument(prepare)
    prepare.add_argument("--source-sha", default="")
    prepare.add_argument("--bootstrap-source-sha", default="")
    prepare.add_argument("--bootstrap-lease-sha", default="")
    prepare.add_argument("--branch", default=INTEGRATION_BRANCH)

    reconcile = subparsers.add_parser("reconcile")
    _add_common_state_argument(reconcile)
    reconcile.add_argument("--agent-uid", type=int, default=65532)
    reconcile.add_argument("omp_command", nargs=argparse.REMAINDER)

    for name in ("privacy", "push-candidate", "push-report", "publish-status"):
        command = subparsers.add_parser(name)
        _add_common_state_argument(command)
    for name in ("preflight-rust", "validate-rust", "validate-node", "validate-python"):
        command = subparsers.add_parser(name)
        _add_common_state_argument(command)
        command.add_argument("--agent-uid", type=int, default=65532)

    render = subparsers.add_parser("render-report")
    _add_common_state_argument(render)
    render.add_argument("--benchmark-summary", type=Path, default=Path(".integration-results/benchmark-summary.json"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    pair_path = _state_path(root, args.pair_state)
    try:
        if args.command == "prepare":
            prepare_shared_candidate(
                root,
                args.source_sha,
                args.branch,
                pair_path,
                args.bootstrap_source_sha,
                args.bootstrap_lease_sha,
            )
        elif args.command == "reconcile":
            command = list(args.omp_command)
            if command and command[0] == "--":
                command = command[1:]
            reconcile_candidate(root, pair_path, command, args.agent_uid)
        elif args.command == "privacy":
            privacy_candidate(root, pair_path)
        elif args.command == "preflight-rust":
            validate_source_rust(root, args.agent_uid)
        elif args.command == "validate-rust":
            validate_candidate_rust(root, pair_path, args.agent_uid)
        elif args.command == "validate-node":
            validate_candidate_node(root, pair_path, args.agent_uid)
        elif args.command == "validate-python":
            validate_candidate_python(root, pair_path, args.agent_uid)
        elif args.command == "push-candidate":
            push_candidate(root, pair_path)
        elif args.command == "render-report":
            summary = args.benchmark_summary if args.benchmark_summary.is_absolute() else root / args.benchmark_summary
            render_report_commit(root, pair_path, summary)
        elif args.command == "push-report":
            push_report(root, pair_path)
        else:
            publish_report_status(root, pair_path)
    except (NightlyError, privacy_gate.PrivacyGateError, benchmark_contract.BenchmarkContractError) as error:
        print(f"nightly integration: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
