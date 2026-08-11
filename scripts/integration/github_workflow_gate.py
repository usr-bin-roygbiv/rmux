#!/usr/bin/env python3
"""Dispatch and wait for the public GitHub Actions validation matrix."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

import nightly

class GateError(RuntimeError):
    pass


_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_WORKFLOW_RE = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml\Z")
_INPUT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_INTEGRATION_REF = "nightly-integration"
_API_ROOT = "https://api.github.com"


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    workflow: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    candidate_input: str = "ref"
    artifact: str | None = None

    def __post_init__(self) -> None:
        if _WORKFLOW_RE.fullmatch(self.workflow) is None:
            raise GateError("workflow manifest contains an invalid workflow filename")
        if _INPUT_RE.fullmatch(self.candidate_input) is None:
            raise GateError("workflow manifest contains an invalid candidate input name")
        if self.artifact is not None and (not self.artifact or "/" in self.artifact or ".." in self.artifact):
            raise GateError("workflow manifest contains an invalid artifact name")


def validate_sha(value: str, name: str = "sha") -> str:
    normalized = value.strip().lower()
    if _SHA_RE.fullmatch(normalized) is None:
        raise GateError(f"{name} must be exactly 40 hexadecimal characters")
    return normalized


def validate_ref(value: str) -> str:
    if value != _INTEGRATION_REF:
        raise GateError(f"dispatch ref must be exactly {_INTEGRATION_REF}")
    return value


def validate_repository(value: str) -> str:
    if _REPOSITORY_RE.fullmatch(value) is None:
        raise GateError("repository must be an owner/name slug")
    return value


def load_workflow_manifest(path: Path) -> tuple[str, list[WorkflowSpec]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError("could not read workflow manifest") from error
    repository = document.get("repository") if isinstance(document, dict) else None
    rows = document.get("workflows") if isinstance(document, dict) else None
    if not isinstance(repository, str):
        raise GateError("workflow manifest must declare the public repository")
    if not isinstance(rows, list) or not rows:
        raise GateError("workflow manifest must contain a nonempty workflows array")
    specs: list[WorkflowSpec] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("workflow"), str):
            raise GateError("workflow manifest row is invalid")
        inputs = row.get("inputs", {})
        if not isinstance(inputs, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in inputs.items()):
            raise GateError("workflow inputs must be a string mapping")
        candidate_input = row.get("candidate_input", "ref")
        if not isinstance(candidate_input, str):
            raise GateError("workflow candidate input must be a string")
        artifact = row.get("artifact")
        if artifact is not None and not isinstance(artifact, str):
            raise GateError("workflow artifact must be a string")
        specs.append(
            WorkflowSpec(
                row["workflow"],
                inputs=inputs,
                candidate_input=candidate_input,
                artifact=artifact,
            )
        )
    if len({spec.workflow for spec in specs}) != len(specs):
        raise GateError("workflow manifest contains duplicate workflows")
    return validate_repository(repository), specs


def load_workflows(path: Path) -> list[WorkflowSpec]:
    return load_workflow_manifest(path)[1]

def _yaml_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _permission_values(lines: Sequence[str], index: int) -> dict[str, str]:
    line = lines[index]
    indent = _yaml_indent(line)
    _, _, inline = line.strip().partition(":")
    if inline.strip():
        if inline.strip() == "{}":
            return {}
        raise GateError("candidate workflow permissions must use an explicit mapping")
    values: dict[str, str] = {}
    for child in lines[index + 1 :]:
        stripped = child.strip()
        if not stripped or stripped.startswith("#"):
            continue
        child_indent = _yaml_indent(child)
        if child_indent <= indent:
            break
        key, separator, value = stripped.partition(":")
        if separator and value.strip():
            values[key.strip()] = value.strip().strip("'\"")
    return values


def audit_candidate_workflow(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise GateError(f"could not read candidate workflow {path.name}") from error
    top_permissions: dict[str, str] | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "${{" in stripped and re.search(r"\bsecrets\s*\.", stripped, re.IGNORECASE):
            raise GateError(f"{path.name} exposes a secret to candidate code")
        if re.search(r"\bgithub[._-]token\b", stripped, re.IGNORECASE):
            raise GateError(f"{path.name} exposes a GitHub token to candidate code")
        key_match = re.match(r"(?:-\s+)?([A-Za-z0-9_.-]+)\s*:", stripped)
        if (
            key_match is not None
            and stripped != "persist-credentials: false"
            and re.search(r"(?:token|credential)", key_match.group(1), re.IGNORECASE)
        ):
            raise GateError(f"{path.name} exposes a token or credential context to candidate code")
        if stripped == "permissions:" or stripped.startswith("permissions: "):
            values = _permission_values(lines, index)
            if _yaml_indent(line) == 0:
                top_permissions = values
            if any(value == "write" for value in values.values()):
                raise GateError(f"{path.name} grants write permission to candidate code")
        if re.match(r"(?:-\s+)?uses:\s*actions/checkout@", stripped):
            use_indent = _yaml_indent(line)
            persistent = True
            for child in lines[index + 1 :]:
                child_stripped = child.strip()
                if not child_stripped or child_stripped.startswith("#"):
                    continue
                child_indent = _yaml_indent(child)
                if child_indent < use_indent or (child_indent <= use_indent and child_stripped.startswith("-")):
                    break
                if child_stripped == "persist-credentials: false":
                    persistent = False
            if persistent:
                raise GateError(f"{path.name} persists a checkout credential")
    if top_permissions is None or top_permissions.get("contents") != "read":
        raise GateError(f"{path.name} must declare top-level contents: read")


def audit_workflow_specs(root: Path, specs: Sequence[WorkflowSpec]) -> None:
    workflow_root = (root / ".github/workflows").resolve()
    for spec in specs:
        path = (workflow_root / spec.workflow).resolve()
        if path.parent != workflow_root:
            raise GateError("workflow path escaped the trusted workflow directory")
        audit_candidate_workflow(path)


def load_pair_state(path: Path) -> tuple[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError("could not read exact nightly pair state") from error
    if not isinstance(document, dict) or document.get("schema") != 2 or document.get("branch") != _INTEGRATION_REF:
        raise GateError("nightly pair state is invalid")
    baseline = validate_sha(str(document.get("baseline_sha", "")), "baseline_sha")
    candidate = validate_sha(str(document.get("candidate_sha", "")), "candidate_sha")
    if baseline == candidate:
        raise GateError("nightly pair SHAs must differ")
    return baseline, candidate


class GitHubClient:
    def __init__(self, repository: str, token: str, timeout_seconds: float = 30.0) -> None:
        self.repository = validate_repository(repository)
        if not token:
            raise GateError("GitHub credential is required")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> bytes:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            _API_ROOT + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cmux-public-integration-gate",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GateError("GitHub Actions API request failed") from error

    def _json_request(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        raw = self._request(method, path, payload)
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GateError("GitHub Actions API returned invalid JSON") from error
        if not isinstance(document, dict):
            raise GateError("GitHub Actions API returned an invalid document")
        return document

    def _workflow_runs(self, workflow: str, ref: str) -> list[dict[str, object]]:
        encoded = urllib.parse.quote(workflow, safe="")
        query = urllib.parse.urlencode(
            {"event": "workflow_dispatch", "branch": ref, "per_page": 100}
        )
        document = self._json_request(
            "GET",
            f"/repos/{self.repository}/actions/workflows/{encoded}/runs?{query}",
        )
        rows = document.get("workflow_runs")
        if not isinstance(rows, list):
            raise GateError("workflow runs response is invalid")
        return [row for row in rows if isinstance(row, dict)]

    def dispatch(
        self,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
        *,
        candidate_input: str = "ref",
        poll_seconds: float = 1.0,
        attempts: int = 60,
    ) -> int:
        if _WORKFLOW_RE.fullmatch(workflow) is None:
            raise GateError("workflow name is invalid")
        confined_ref = validate_ref(ref)
        if _INPUT_RE.fullmatch(candidate_input) is None:
            raise GateError("candidate input name is invalid")
        candidate = validate_sha(inputs.get(candidate_input, ""), "candidate_sha")
        if attempts < 1 or attempts > 120 or poll_seconds < 0:
            raise GateError("workflow dispatch correlation policy is invalid")

        before = {
            row["id"]
            for row in self._workflow_runs(workflow, confined_ref)
            if type(row.get("id")) is int and int(row["id"]) > 0
        }
        encoded = urllib.parse.quote(workflow, safe="")
        self._request(
            "POST",
            f"/repos/{self.repository}/actions/workflows/{encoded}/dispatches",
            {"ref": confined_ref, "inputs": inputs},
        )
        for attempt in range(1, attempts + 1):
            matches = [
                row
                for row in self._workflow_runs(workflow, confined_ref)
                if type(row.get("id")) is int
                and int(row["id"]) > 0
                and row["id"] not in before
                and row.get("event") == "workflow_dispatch"
                and str(row.get("head_sha", "")).lower() == candidate
                and row.get("head_branch") == confined_ref
            ]
            if len(matches) == 1:
                return int(matches[0]["id"])
            if len(matches) > 1:
                raise GateError("workflow dispatch produced ambiguous exact-SHA runs")
            if attempt < attempts:
                time.sleep(poll_seconds)
        raise GateError("workflow dispatch run was not observable")

    def get_run(self, run_id: int) -> dict[str, object]:
        return self._json_request("GET", f"/repos/{self.repository}/actions/runs/{run_id}")

    def download_artifact(self, run_id: int, artifact: str, destination: Path) -> None:
        query = urllib.parse.urlencode({"name": artifact, "per_page": 10})
        document = self._json_request(
            "GET",
            f"/repos/{self.repository}/actions/runs/{run_id}/artifacts?{query}",
        )
        rows = document.get("artifacts")
        matches = [
            row
            for row in rows if isinstance(row, dict) and row.get("name") == artifact and row.get("expired") is False
        ] if isinstance(rows, list) else []
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise GateError("required workflow artifact was not uniquely available")
        archive = self._request(
            "GET",
            f"/repos/{self.repository}/actions/artifacts/{matches[0]['id']}/zip",
        )
        destination.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                members = [item for item in bundle.infolist() if not item.is_dir()]
                if not members:
                    raise GateError("workflow artifact is empty")
                for member in members:
                    relative = Path(member.filename)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise GateError("workflow artifact contains an unsafe path")
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.read(member))
        except zipfile.BadZipFile as error:
            raise GateError("workflow artifact is not a valid zip archive") from error


def _resolved_inputs(spec: WorkflowSpec, baseline_sha: str, candidate_sha: str) -> dict[str, str]:
    replacements = {
        "{baseline_sha}": baseline_sha,
        "{candidate_sha}": candidate_sha,
        "true": "true",
        "false": "false",
        "full": "full",
        "macos-15": "macos-15",
        "macos-26": "macos-26",
        "cmuxTests/SidebarSelectedWorkspaceColorTests/testLightModeUsesConfiguredSelectedWorkspaceBackgroundColor": "cmuxTests/SidebarSelectedWorkspaceColorTests/testLightModeUsesConfiguredSelectedWorkspaceBackgroundColor",
    }
    resolved: dict[str, str] = {}
    for key, value in spec.inputs.items():
        if value not in replacements:
            raise GateError("workflow manifest input must use an approved immutable value")
        resolved[key] = replacements[value]
    return resolved


def dispatch_and_wait(
    client: GitHubClient,
    specs: Sequence[WorkflowSpec],
    *,
    ref: str,
    candidate_sha: str,
    baseline_sha: str,
    artifact_dir: Path,
    poll_seconds: float = 15.0,
    timeout_seconds: float = 6 * 60 * 60,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    confined_ref = validate_ref(ref)
    candidate = validate_sha(candidate_sha, "candidate_sha")
    baseline = validate_sha(baseline_sha, "baseline_sha")
    if candidate == baseline:
        raise GateError("baseline and candidate SHAs must differ")
    if not specs:
        raise GateError("workflow list is empty")

    run_ids: dict[str, int] = {}
    for spec in specs:
        inputs = _resolved_inputs(spec, baseline, candidate)
        if inputs.get(spec.candidate_input) != candidate:
            raise GateError(
                f"{spec.workflow} does not receive the exact candidate input "
                f"{spec.candidate_input}"
            )
        run_id = client.dispatch(
            spec.workflow,
            confined_ref,
            inputs,
            candidate_input=spec.candidate_input,
        )
        run_ids[spec.workflow] = run_id
        print(f"dispatched {spec.workflow} as run {run_id}")

    deadline = time.monotonic() + timeout_seconds
    pending = {spec.workflow: spec for spec in specs}
    while pending:
        if time.monotonic() >= deadline:
            raise GateError("timed out waiting for GitHub Actions workflows")
        for workflow, spec in list(pending.items()):
            run_id = run_ids[workflow]
            run = client.get_run(run_id)
            if run.get("event") != "workflow_dispatch":
                raise GateError(f"{workflow} run used an unexpected event")
            if str(run.get("head_sha", "")).lower() != candidate:
                raise GateError(f"{workflow} run did not use the exact candidate SHA")
            status = run.get("status")
            if status != "completed":
                continue
            if run.get("conclusion") != "success":
                raise GateError(f"{workflow} run did not succeed")
            if spec.artifact:
                client.download_artifact(run_id, spec.artifact, artifact_dir)
            print(f"completed {workflow} as run {run_id}")
            del pending[workflow]
        if pending:
            sleep(poll_seconds)
    return run_ids


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=_INTEGRATION_REF)
    parser.add_argument("--pair-state", type=Path, default=Path(".integration-results/nightly-pair.json"))
    parser.add_argument("--manifest", type=Path, default=Path("integration/nightly/workflows.json"))
    parser.add_argument("--artifact-dir", type=Path, default=Path(".integration-results"))
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=6 * 60 * 60)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path.cwd().resolve()
    pair_path = args.pair_state if args.pair_state.is_absolute() else root / args.pair_state
    manifest_path = args.manifest if args.manifest.is_absolute() else root / args.manifest
    artifact_dir = args.artifact_dir if args.artifact_dir.is_absolute() else root / args.artifact_dir
    try:
        nightly.attest_trusted_state(
            root,
            pair_path,
            head_key="candidate_sha",
            required_markers=("candidate_published_sha",),
        )
        repository, specs = load_workflow_manifest(manifest_path)
        audit_workflow_specs(root, specs)
        baseline_sha, candidate_sha = load_pair_state(pair_path)
        credential = os.environ.get("GITHUB_TOKEN", "")
        client = GitHubClient(repository, credential)
        dispatch_and_wait(
            client,
            specs,
            ref=args.ref,
            candidate_sha=candidate_sha,
            baseline_sha=baseline_sha,
            artifact_dir=artifact_dir,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except (GateError, nightly.NightlyError) as error:
        print(f"GitHub Actions gate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
