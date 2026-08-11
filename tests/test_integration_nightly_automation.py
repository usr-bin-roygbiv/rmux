#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "integration"))

import benchmark_contract
import github_workflow_gate
import nightly
import privacy_gate


class PrivacyGateContractTests(unittest.TestCase):
    def test_sensitive_categories_are_rejected_without_echoing_values(self) -> None:
        identity_marker = "synthetic-person-marker"
        fixtures = {
            "identity": identity_marker,
            "email": "person" + "@" + "example.test",
            "home": "/home/" + "developer/project",
            "tailnet": "builder" + ".ts" + ".net",
            "private_host": "https://ci" + ".internal/api",
            "cluster": "namespace" + ": product-ci",
            "registry": "registry" + ".corp.example/cmux/build:1",
            "credential": "ACCESS_TOKEN" + "=gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789",
        }
        for expected_rule, value in fixtures.items():
            with self.subTest(expected_rule=expected_rule):
                findings = privacy_gate.scan_text(
                    Path("integration/change.txt"), value, denylist=(identity_marker,)
                )
                self.assertTrue(any(item.rule == expected_rule for item in findings), findings)
                rendered = privacy_gate.render_findings(findings)
                self.assertNotIn(value, rendered)

    def test_generic_secret_environment_references_are_allowed(self) -> None:
        safe = "\n".join(
            (
                "GITHUB_TOKEN: $" + "{{ secrets.GITHUB_TOKEN }}",
                "AI_PROVIDER_API_KEY: $" + "{{ secrets.AI_PROVIDER_API_KEY }}",
                "OPENAI_API_KEY: $" + "{{ secrets.OPENAI_API_KEY }}",
            )
        )
        self.assertEqual([], privacy_gate.scan_text(Path(".woodpecker.yml"), safe))

    def test_synthetic_local_token_labels_are_allowed(self) -> None:
        safe = 'token = f"PERF_{index:03d}"'
        self.assertEqual([], privacy_gate.scan_text(Path("scripts/perf.py"), safe))
    def test_manifest_and_diff_modes_only_return_tracked_public_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "integration").mkdir()
            manifest = root / "integration" / "public-files.txt"
            manifest.write_text("safe.txt\nmissing.txt\n../escape.txt\n", encoding="utf-8")
            (root / "safe.txt").write_text("public\n", encoding="utf-8")
            with self.assertRaises(privacy_gate.PrivacyGateError):
                privacy_gate.paths_from_manifest(root, manifest)

            manifest.write_text("safe.txt\n", encoding="utf-8")
            self.assertEqual([Path("safe.txt")], privacy_gate.paths_from_manifest(root, manifest))
            self.assertEqual(
                [Path("safe.txt")],
                privacy_gate.intersect_manifest_paths(
                    [Path("safe.txt"), Path("not-listed.txt")],
                    [Path("safe.txt")],
                ),
            )


class WorkflowDispatchContractTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, target_sha: str) -> None:
            self.target_sha = target_sha
            self.dispatched: list[tuple[str, str, dict[str, str], str]] = []
            self.downloaded: list[tuple[int, str, Path]] = []

        def dispatch(
            self,
            workflow: str,
            ref: str,
            inputs: dict[str, str],
            *,
            candidate_input: str = "ref",
        ) -> int:
            self.dispatched.append((workflow, ref, inputs, candidate_input))
            return len(self.dispatched)

        def get_run(self, run_id: int) -> dict[str, object]:
            return {
                "id": run_id,
                "head_sha": self.target_sha,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
            }

        def download_artifact(self, run_id: int, artifact: str, destination: Path) -> None:
            self.downloaded.append((run_id, artifact, destination))

    def test_dispatches_every_workflow_at_the_confined_ref_and_verifies_sha(self) -> None:
        sha = "a" * 40
        baseline = "b" * 40
        client = self.FakeClient(sha)
        specs = [
            github_workflow_gate.WorkflowSpec("ci.yml", inputs={"ref": "{candidate_sha}"}),
            github_workflow_gate.WorkflowSpec(
                "cmux-tui.yml",
                inputs={
                    "commit": "{candidate_sha}",
                    "mode": "full",
                    "request_id": "{candidate_sha}",
                },
                candidate_input="commit",
            ),
            github_workflow_gate.WorkflowSpec(
                "perf-activation.yml",
                inputs={
                    "baseline_sha": "{baseline_sha}",
                    "candidate_sha": "{candidate_sha}",
                    "ref": "{candidate_sha}",
                    "paired_only": "true",
                },
                artifact="integration-benchmark",
            ),
        ]
        github_workflow_gate.dispatch_and_wait(
            client,
            specs,
            ref="nightly-integration",
            candidate_sha=sha,
            baseline_sha=baseline,
            artifact_dir=Path("results"),
            poll_seconds=0,
            sleep=lambda _: None,
        )
        self.assertEqual(
            [
                ("ci.yml", "nightly-integration", {"ref": sha}, "ref"),
                (
                    "cmux-tui.yml",
                    "nightly-integration",
                    {"commit": sha, "mode": "full", "request_id": sha},
                    "commit",
                ),
                (
                    "perf-activation.yml",
                    "nightly-integration",
                    {"baseline_sha": baseline, "candidate_sha": sha, "ref": sha, "paired_only": "true"},
                    "ref",
                ),
            ],
            client.dispatched,
        )
        self.assertEqual([(3, "integration-benchmark", Path("results"))], client.downloaded)

    def test_sha_mismatch_fails_the_gate(self) -> None:
        client = self.FakeClient("c" * 40)
        with self.assertRaises(github_workflow_gate.GateError):
            github_workflow_gate.dispatch_and_wait(
                client,
                [github_workflow_gate.WorkflowSpec("ci.yml", inputs={"ref": "{candidate_sha}"})],
                ref="nightly-integration",
                candidate_sha="a" * 40,
                baseline_sha="b" * 40,
                artifact_dir=Path("results"),
                poll_seconds=0,
                sleep=lambda _: None,
            )

    def test_branch_and_ref_confinement(self) -> None:
        self.assertEqual("nightly-integration", github_workflow_gate.validate_ref("nightly-integration"))
        for value in ("main", "refs/heads/nightly-integration", "nightly-integration/extra", "pull/12/head"):
            with self.subTest(value=value):
                with self.assertRaises(github_workflow_gate.GateError):
                    github_workflow_gate.validate_ref(value)


class BenchmarkAndReportContractTests(unittest.TestCase):
    def test_prior_nightly_comparison_is_sorted_and_small(self) -> None:
        previous = {
            "schema": 1,
            "candidate_sha": "1" * 40,
            "metrics": {"cpu_percent": 10.0, "memory_bytes": 100},
        }
        current = {
            "schema": 1,
            "baseline_sha": "2" * 40,
            "candidate_sha": "3" * 40,
            "metrics": {"memory_bytes": 90, "cpu_percent": 12.5},
        }
        compared = benchmark_contract.compare_with_previous(previous, current)
        self.assertEqual(["cpu_percent", "memory_bytes"], list(compared["deltas_from_previous"]))
        self.assertEqual(2.5, compared["deltas_from_previous"]["cpu_percent"])
        self.assertEqual(-10, compared["deltas_from_previous"]["memory_bytes"])
        encoded = benchmark_contract.encode_json(compared)
        self.assertLess(len(encoded), 4096)
        self.assertEqual(encoded, benchmark_contract.encode_json(compared))

    def test_report_update_owns_one_report_and_one_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = {
                "schema": 1,
                "baseline_sha": "2" * 40,
                "candidate_sha": "3" * 40,
                "metrics": {"cpu_percent": 12.5},
                "deltas_from_baseline": {"cpu_percent": 2.5},
                "deltas_from_previous": {"cpu_percent": 1.0},
            }
            paths = nightly.update_public_report(
                root,
                summary,
                commits=[("abc1234", "Product change (#9023)")],
                manifest={"pull_requests": []},
            )
            self.assertEqual(
                {
                    root / "integration/nightly/report.md",
                    root / "integration/nightly/benchmark-state.json",
                },
                set(paths),
            )
            self.assertEqual([], list(root.glob("integration/nightly/report-*.md")))
            self.assertIn("abc1234", (root / "integration/nightly/report.md").read_text(encoding="utf-8"))


class NightlyOrderingContractTests(unittest.TestCase):
    class FakeOperations:
        def __init__(self, fail_validation: bool = False) -> None:
            self.calls: list[str] = []
            self.fail_validation = fail_validation

        def fetch_upstream(self) -> str:
            self.calls.append("fetch")
            return "b" * 40

        def create_rebased_candidate(self, baseline_sha: str) -> str:
            self.calls.append("rebase")
            return "a" * 40

        def run_omp(self, candidate_sha: str) -> str:
            self.calls.append("omp")
            return "c" * 40

        def privacy_gate(self, baseline_sha: str, candidate_sha: str) -> None:
            self.calls.append("privacy")

        def validate_product(self, candidate_sha: str) -> None:
            self.calls.append("validate")
            if self.fail_validation:
                raise RuntimeError("validation failed")

        def push_candidate(self, candidate_sha: str) -> None:
            self.calls.append("push")

    def test_failure_stops_before_push(self) -> None:
        operations = self.FakeOperations(fail_validation=True)
        with self.assertRaises(RuntimeError):
            nightly.prepare_candidate(operations)
        self.assertEqual(["fetch", "rebase", "omp", "privacy", "validate"], operations.calls)

    def test_success_pushes_only_after_validation(self) -> None:
        operations = self.FakeOperations()
        nightly.prepare_candidate(operations)
        self.assertEqual(["fetch", "rebase", "omp", "privacy", "validate", "push"], operations.calls)


class NightlyReconciliationContractTests(unittest.TestCase):
    def test_empty_product_manifest_keeps_verified_candidate_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate"
            candidate.mkdir()
            pair = {"candidate_sha": "a" * 40}
            with (
                mock.patch.object(nightly.os, "geteuid", return_value=0),
                mock.patch.object(nightly, "load_pair_state", return_value=pair),
                mock.patch.object(nightly, "_verify_candidate", return_value=candidate),
                mock.patch.object(privacy_gate, "paths_from_manifest", return_value=[]),
                mock.patch.object(nightly, "_run") as run_agent,
            ):
                reconciled = nightly.reconcile_candidate(
                    root,
                    root / "pair.json",
                    ["agent"],
                    agent_uid=65532,
                )

        self.assertEqual(pair["candidate_sha"], reconciled)
        run_agent.assert_not_called()

class PublicAutomationShapeTests(unittest.TestCase):
    def test_manifest_has_exact_canonical_twenty(self) -> None:
        manifest = json.loads((ROOT / "integration/nightly/pr-manifest.json").read_text(encoding="utf-8"))
        rows = manifest["pull_requests"]
        self.assertEqual(20, len(rows))
        self.assertEqual(
            {
                "usr-bin-roygbiv": 14,
                "iarbpairs": 4,
                "wolfiesch": 2,
            },
            {handle: sum(row["author"] == handle for row in rows) for handle in {row["author"] for row in rows}},
        )
        by_number = {row["number"]: row for row in rows}
        self.assertEqual("no-op/superseded", by_number[8668]["classification"])
        self.assertEqual("benchmark-only", by_number[8762]["classification"])
        self.assertEqual("superseded-by-8999", by_number[8996]["classification"])

    def test_woodpecker_uses_pinned_public_images_and_cron_only_omp(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        allowed = {
            "public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7",
            "public.ecr.aws/docker/library/node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b",
            "public.ecr.aws/docker/library/rust:1.95.0-bookworm@sha256:6258907abe69656e41cd992e0b705cdcfabcbbe3db374f92ed2d47121282d4a1",
            "quay.io/woodpeckerci/plugin-git:2.6.5@sha256:2b34ecbbfd2f318405bd5d2765a750f6b02dae5dfb8707e16c943b9faeef5fc5",
            "${RMUX_CI_BUILDER_IMAGE}",
            "${RMUX_CI_PULL_REGISTRY}/linkedin-bot/rmux-ci:sha-${CI_COMMIT_SHA}",
        }
        images = {
            line.split("image:", 1)[1].strip()
            for line in pipeline.splitlines()
            if line.strip().startswith("image:")
        }
        self.assertTrue(images)
        self.assertLessEqual(images, allowed)
        nightly_block = pipeline.split("reconcile-candidate:", 1)[1].split("  privacy-candidate:", 1)[0]
        self.assertIn("event: cron", pipeline)
        self.assertIn("@oh-my-pi/pi-coding-agent@17.1.6", nightly_block)
        self.assertIn("gpt-proxy/gpt-5.6-sol", nightly_block)
        self.assertIn("--thinking=xhigh", nightly_block)
        for flag in ("--no-session", "--no-extensions", "--no-skills", "--no-rules", "--no-prewalk"):
            self.assertIn(flag, nightly_block)
        self.assertNotIn("browser", nightly_block)
        self.assertNotIn("task", nightly_block)

    def test_workflow_gate_covers_the_secretless_non_release_matrix(self) -> None:
        manifest = json.loads((ROOT / "integration/nightly/workflows.json").read_text(encoding="utf-8"))
        workflows = {row["workflow"] for row in manifest["workflows"]}
        self.assertEqual(
            {
                "ci.yml",
                "ci-macos-compat.yml",
                "cmux-tui.yml",
                "perf-activation.yml",
                "test-depot.yml",
                "test-ios.yml",
                "tmux-corpus.yml",
            },
            workflows,
        )
        self.assertNotIn("release.yml", workflows)
        self.assertNotIn("nightly.yml", workflows)

    def test_static_pr_body_links_only_to_updating_report(self) -> None:
        body = (ROOT / "integration/nightly/pull-request-body.md").read_text(encoding="utf-8")
        self.assertEqual(1, body.count("report.md"))
        self.assertNotIn("actions/runs/", body)
        automation = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "scripts" / "integration").glob("*.py")
        )
        for forbidden in ("/pulls", "createPullRequest", "closePullRequest", "gh pr"):
            self.assertNotIn(forbidden, automation)


if __name__ == "__main__":
    unittest.main()
