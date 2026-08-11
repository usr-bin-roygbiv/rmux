#!/usr/bin/env python3
"""Fail closed when public integration files contain private operational data."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class PrivacyGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    rule: str


_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_HOME_RE = re.compile(r"(?i)(?:/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|[A-Z]:\\Users\\[^\\\s]+)")
_TAILNET_RE = re.compile(r"(?i)\b(?:" + "tail" + "scale" + r"|[a-z0-9-]+\.ts\.net)\b")
_PRIVATE_HOST_RE = re.compile(
    r"(?i)(?:https?://[^\s/'\"]+\.(?:internal|local|lan)(?::\d+)?|"
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b)"
)
_CLUSTER_RE = re.compile(
    r"(?i)(?:\b(?:" + "kube" + "config|" + "kube" + "ctl|" + "kuber" + "netes" + r")\b|"
    r"^\s*(?:namespace|cluster|context)\s*:\s*[A-Za-z0-9._-]+)",
    re.MULTILINE,
)
_REGISTRY_RE = re.compile(
    r"(?i)\b(?:registry\.(?!npmjs\.org\b|github\.com\b|docker\.com\b)[A-Za-z0-9.-]+|"
    r"[A-Za-z0-9.-]+\.(?:internal|corp|lan)(?::\d+)?/[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?)"
)
_CREDENTIAL_RE = re.compile(
    r"(?im)^\s*(?:export\s+)?(?P<key>(?:[A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API_KEY|PRIVATE_KEY)|from_secret))\s*[:=]\s*"
    r"(?P<value>[^\s#]+)"
)
_TOKEN_VALUE_RE = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"
)
_CREDENTIAL_URL_RE = re.compile(r"(?i)https?://[^\s/@:'\"]+:[^\s/@'\"]+@[^\s/'\"]+")
_ALLOWED_EMAIL_SUFFIXES = ("@users.noreply.github.com", "@example.invalid")
_TEMPLATE_PREFIXES = ("${", "{{", "secrets.", "env.", "from_secret")
_DENYLIST_ENV = "CMUX_PRIVACY_DENYLIST_JSON"
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def load_runtime_denylist(*, required: bool = False) -> tuple[str, ...]:
    raw = os.environ.get(_DENYLIST_ENV, "")
    if not raw:
        if required:
            raise PrivacyGateError("runtime privacy denylist is required")
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PrivacyGateError("runtime privacy denylist is invalid") from error
    if (
        not isinstance(values, list)
        or not values
        or len(values) > 100
        or not all(isinstance(value, str) and 3 <= len(value) <= 512 for value in values)
    ):
        raise PrivacyGateError("runtime privacy denylist is invalid")
    return tuple(values)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _add_matches(findings: list[Finding], path: Path, text: str, rule: str, pattern: re.Pattern[str]) -> None:
    for match in pattern.finditer(text):
        findings.append(Finding(path=path, line=_line_number(text, match.start()), rule=rule))


def _credential_findings(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in _CREDENTIAL_RE.finditer(text):
        if match.group("key").lower() == "from_secret":
            continue
        if match.group("key") != match.group("key").upper():
            continue
        value = match.group("value").strip("'\"")
        if not value or value.startswith(_TEMPLATE_PREFIXES):
            continue
        if value in {"required", "optional", "redacted", "REDACTED"}:
            continue
        findings.append(Finding(path=path, line=_line_number(text, match.start()), rule="credential"))
    _add_matches(findings, path, text, "credential", _TOKEN_VALUE_RE)
    _add_matches(findings, path, text, "credential", _CREDENTIAL_URL_RE)
    return findings


def scan_text(path: Path, text: str, denylist: Iterable[str] = ()) -> list[Finding]:
    findings: list[Finding] = []
    for marker in denylist:
        pattern = re.compile(re.escape(marker), re.IGNORECASE)
        _add_matches(findings, path, text, "identity", pattern)
    for match in _EMAIL_RE.finditer(text):
        if match.group(0).lower().endswith(_ALLOWED_EMAIL_SUFFIXES):
            continue
        findings.append(Finding(path=path, line=_line_number(text, match.start()), rule="email"))
    _add_matches(findings, path, text, "home", _HOME_RE)
    _add_matches(findings, path, text, "tailnet", _TAILNET_RE)
    _add_matches(findings, path, text, "private_host", _PRIVATE_HOST_RE)
    _add_matches(findings, path, text, "cluster", _CLUSTER_RE)
    _add_matches(findings, path, text, "registry", _REGISTRY_RE)
    findings.extend(_credential_findings(path, text))
    return sorted(set(findings), key=lambda item: (item.path.as_posix(), item.line, item.rule))


def render_findings(findings: Iterable[Finding]) -> str:
    rows = [f"{item.path.as_posix()}:{item.line}: blocked category {item.rule}" for item in findings]
    return "\n".join(rows)


def _safe_relative_path(raw: str) -> Path:
    path = Path(raw.strip())
    if not raw.strip() or path.is_absolute() or ".." in path.parts:
        raise PrivacyGateError("public file manifest contains an unsafe path")
    return path


def paths_from_manifest(root: Path, manifest: Path) -> list[Path]:
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PrivacyGateError("could not read public file manifest") from error
    paths: list[Path] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        relative = _safe_relative_path(stripped)
        if not (root / relative).is_file():
            raise PrivacyGateError(f"manifest path is not a file: {relative.as_posix()}")
        paths.append(relative)
    if len(paths) != len(set(paths)):
        raise PrivacyGateError("public file manifest contains duplicate paths")
    return sorted(paths)


def intersect_manifest_paths(changed: Iterable[Path], manifested: Iterable[Path]) -> list[Path]:
    allowed = set(manifested)
    return sorted(path for path in changed if path in allowed)


def _git(root: Path, args: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise PrivacyGateError("git could not enumerate public integration files") from error
    return completed.stdout


def changed_paths(root: Path, base: str, head: str = "HEAD") -> list[Path]:
    if not base or base.startswith("-") or not head or head.startswith("-"):
        raise PrivacyGateError("invalid git revision")
    output = _git(root, ["diff", "--name-only", "--diff-filter=ACMR", f"{base}...{head}", "--"])
    return sorted(_safe_relative_path(line) for line in output.splitlines() if line.strip())

def scan_unified_diff(diff: str, denylist: Iterable[str] = ()) -> list[Finding]:
    findings: list[Finding] = []
    current_path: Path | None = None
    new_line = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            _, candidate = line.rsplit(" b/", 1)
            current_path = _safe_relative_path(candidate)
            in_hunk = False
            continue
        if line == "GIT binary patch" or line.startswith("Binary files "):
            if current_path is None:
                raise PrivacyGateError("binary diff path is unavailable")
            findings.append(Finding(path=current_path, line=0, rule="binary"))
            in_hunk = False
            continue
        if line.startswith("+++ "):
            raw_path = line[4:]
            current_path = None if raw_path == "/dev/null" else _safe_relative_path(raw_path.removeprefix("b/"))
            in_hunk = False
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk or current_path is None:
            continue
        if line.startswith("+"):
            for finding in scan_text(current_path, line[1:], denylist=denylist):
                findings.append(Finding(path=current_path, line=new_line, rule=finding.rule))
            new_line += 1
        elif line.startswith(" "):
            new_line += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        else:
            in_hunk = False
    return sorted(set(findings), key=lambda item: (item.path.as_posix(), item.line, item.rule))


def scan_diff(root: Path, base: str, head: str = "HEAD", denylist: Iterable[str] = ()) -> list[Finding]:
    if not base or base.startswith("-") or not head or head.startswith("-"):
        raise PrivacyGateError("invalid git revision")
    diff = _git(
        root,
        ["diff", "--no-ext-diff", "--no-renames", "--unified=0", f"{base}...{head}", "--"],
    )
    return scan_unified_diff(diff, denylist=denylist)


def tracked_paths(root: Path) -> list[Path]:
    output = _git(root, ["ls-files", "-z"])
    return sorted(_safe_relative_path(item) for item in output.split("\0") if item)


def scan_paths(root: Path, paths: Iterable[Path], denylist: Iterable[str] = ()) -> list[Finding]:
    findings: list[Finding] = []
    for relative in sorted(set(paths)):
        absolute = root / relative
        try:
            data = absolute.read_bytes()
        except OSError as error:
            raise PrivacyGateError(f"could not read tracked file: {relative.as_posix()}") from error
        if b"\0" in data:
            findings.append(Finding(path=relative, line=0, rule="binary"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding(path=relative, line=0, rule="binary"))
            continue
        findings.extend(scan_text(relative, text, denylist=denylist))
    return sorted(findings, key=lambda item: (item.path.as_posix(), item.line, item.rule))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=Path("integration/nightly/public-files.txt"))
    parser.add_argument("--diff-base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--all-tracked", action="store_true")
    parser.add_argument("--require-runtime-denylist", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    try:
        denylist = load_runtime_denylist(required=args.require_runtime_denylist)
        manifested = paths_from_manifest(root, manifest_path)
        if args.all_tracked:
            selected = intersect_manifest_paths(tracked_paths(root), manifested)
            findings = scan_paths(root, selected, denylist=denylist)
        elif args.diff_base:
            selected = changed_paths(root, args.diff_base, args.head)
            findings = scan_diff(root, args.diff_base, args.head, denylist=denylist)
        else:
            selected = manifested
            findings = scan_paths(root, selected, denylist=denylist)
    except PrivacyGateError as error:
        print(f"privacy gate: {error}", file=sys.stderr)
        return 2
    if findings:
        print(render_findings(findings), file=sys.stderr)
        return 1
    print(f"privacy gate: {len(selected)} public file(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
