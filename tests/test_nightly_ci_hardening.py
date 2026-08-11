#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "integration"))

import github_workflow_gate
import nightly
import privacy_gate


class BranchAndDispatchHardeningTests(unittest.TestCase):
    def test_existing_public_branch_name_is_used_everywhere(self) -> None:
        self.assertEqual("nightly-integration", nightly.INTEGRATION_BRANCH)
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        self.assertNotIn("branch: integration/nightly", pipeline)
        self.assertIn("cron: nightly-integration-daily", pipeline)
        self.assertIn("branch: main", pipeline)
        body = (ROOT / "integration/nightly/pull-request-body.md").read_text(encoding="utf-8")
        self.assertIn("/blob/nightly-integration/integration/nightly/report.md", body)

    def test_private_native_control_bootstraps_only_the_exact_initial_lease(self) -> None:
        lease = "a" * 40
        moved_lease = "c" * 40
        seed = "b" * 40
        with patch.dict(os.environ, {}, clear=True):
            args = nightly.parse_args(["prepare"])
        self.assertEqual("", args.source_sha)
        self.assertEqual("", args.bootstrap_source_sha)
        self.assertEqual("", args.bootstrap_lease_sha)
        self.assertEqual("nightly-integration", args.branch)
        self.assertEqual(lease, nightly.select_candidate_seed("", lease))
        self.assertEqual(seed, nightly.select_candidate_seed(seed, lease, seed, moved_lease))
        self.assertEqual(seed, nightly.select_candidate_seed("", lease, seed, lease))
        self.assertEqual(moved_lease, nightly.select_candidate_seed("", moved_lease, seed, lease))
        with self.assertRaisesRegex(nightly.NightlyError, "bootstrap candidate seed is incomplete"):
            nightly.select_candidate_seed("", lease, seed, "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual({}, nightly.optional_control_pipeline_hashes(root))
            control = root / ".woodpecker-control.yml"
            control.write_text("steps: {}\n", encoding="utf-8")
            hashes = nightly.optional_control_pipeline_hashes(root)
            self.assertEqual({"@control-pipeline"}, set(hashes))
            self.assertRegex(hashes["@control-pipeline"], r"^[0-9a-f]{64}$")

    def test_every_dispatched_workflow_receives_exact_candidate_input(self) -> None:
        manifest = json.loads((ROOT / "integration/nightly/workflows.json").read_text(encoding="utf-8"))
        for row in manifest["workflows"]:
            with self.subTest(workflow=row["workflow"]):
                candidate_input = row.get("candidate_input", "ref")
                inputs = row.get("inputs", {})
                self.assertEqual("{candidate_sha}", inputs.get(candidate_input))
                if row["workflow"] == "cmux-tui.yml":
                    self.assertEqual("commit", candidate_input)
                    self.assertEqual("full", inputs.get("mode"))
                    self.assertEqual("{candidate_sha}", inputs.get("request_id"))

    def test_dispatched_workflows_pin_every_checkout_to_candidate_input(self) -> None:
        workflows = {
            "ci.yml": ("ref", "ref: ${{ inputs.ref || github.sha }}"),
            "ci-macos-compat.yml": ("ref", "ref: ${{ inputs.ref || github.sha }}"),
            "cmux-tui.yml": ("commit", "ref: ${{ inputs.commit }}"),
            "ios-streamed-validate.yml": ("ref", "ref: ${{ inputs.ref || github.sha }}"),
            "tmux-corpus.yml": ("ref", "ref: ${{ inputs.ref || github.sha }}"),
        }
        for name, (input_name, checkout_ref) in workflows.items():
            with self.subTest(workflow=name):
                text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
                self.assertIn(f"      {input_name}:\n", text.split("concurrency:", 1)[0])
                checkout_blocks = text.split("uses: actions/checkout@")[1:]
                self.assertTrue(checkout_blocks)
                for block in checkout_blocks:
                    checkout_step = block.split("\n      - ", 1)[0]
                    self.assertIn(checkout_ref, checkout_step)
                if name == "cmux-tui.yml":
                    self.assertNotIn("inputs.ref", text)

    def test_cmux_tui_valgrind_matrix_matches_declared_shards(self) -> None:
        workflow = (ROOT / ".github/workflows/cmux-tui.yml").read_text(encoding="utf-8")
        block = workflow.split("  valgrind-leak-check-shard:", 1)[1].split(
            "\n  valgrind-leak-check:", 1
        )[0]
        matrix = re.search(r"(?m)^        shard: \[([^\]]+)\]$", block)
        declared = re.search(r'known_shards = \{([^}]+)\}', block)
        self.assertIsNotNone(matrix)
        self.assertIsNotNone(declared)
        matrix_shards = {value.strip() for value in matrix.group(1).split(",")}
        declared_shards = {
            value.strip().strip("\"'") for value in declared.group(1).split(",")
        }
        self.assertEqual(matrix_shards, declared_shards)

    def test_existing_ref_workflows_use_sha_fallback(self) -> None:
        for name in ("test-depot.yml", "test-e2e.yml", "test-ios.yml", "perf-activation.yml"):
            with self.subTest(workflow=name):
                text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
                self.assertNotIn("inputs.ref || github.ref", text)
                self.assertIn("inputs.ref || github.sha", text)

    def test_compatibility_matrix_defaults_to_standard_public_sonoma(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-macos-compat.yml").read_text(encoding="utf-8")
        self.assertNotIn("macos-14-large", workflow)
        self.assertIn("vars.MACOS_RUNNER_14 || 'macos-14'", workflow)

    def test_every_dispatched_job_targets_rmux_and_public_runners(self) -> None:
        manifest = json.loads((ROOT / "integration/nightly/workflows.json").read_text(encoding="utf-8"))
        self.assertEqual("usr-bin-roygbiv/rmux", manifest["repository"])
        for row in manifest["workflows"]:
            with self.subTest(workflow=row["workflow"]):
                workflow = (ROOT / ".github/workflows" / row["workflow"]).read_text(encoding="utf-8")
                runs_on = [line for line in workflow.splitlines() if line.strip().startswith("runs-on:")]
                self.assertTrue(runs_on)
                self.assertFalse(
                    any(
                        marker in line
                        for line in runs_on
                        for marker in ("self-hosted", "warp-", "depot-", "tart-")
                    )
                )
                if row["workflow"] == "cmux-tui.yml":
                    self.assertNotIn("blacksmith-", workflow)
                runner = row.get("inputs", {}).get("runner")
                if "inputs.runner" in workflow:
                    self.assertIn(runner, {"macos-15", "macos-26"})


class PublicGitBoundaryTests(unittest.TestCase):
    @staticmethod
    def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    def test_candidate_rebase_isolated_from_private_config_hooks_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            candidate = root / "candidate"
            control.mkdir()
            self.git(control, "init", "--initial-branch=main")
            public_env = dict(os.environ)
            public_env.update(
                {
                    "GIT_AUTHOR_NAME": "cmux integration",
                    "GIT_AUTHOR_EMAIL": "cmux-integration@users.noreply.github.com",
                    "GIT_COMMITTER_NAME": "cmux integration",
                    "GIT_COMMITTER_EMAIL": "cmux-integration@users.noreply.github.com",
                }
            )
            (control / "product.txt").write_text("base\n", encoding="utf-8")
            self.git(control, "add", "product.txt")
            self.git(control, "commit", "-m", "base", env=public_env)
            baseline = self.git(control, "rev-parse", "HEAD")

            contributor_env = dict(public_env)
            contributor_env.update(
                {
                    "GIT_AUTHOR_NAME": "iarbpairs",
                    "GIT_AUTHOR_EMAIL": "235640537+iarbpairs@users.noreply.github.com",
                    "GIT_COMMITTER_NAME": "private control",
                    "GIT_COMMITTER_EMAIL": "private-control@example.invalid",
                }
            )
            (control / "product.txt").write_text("contributor\n", encoding="utf-8")
            self.git(control, "commit", "-am", "contributor change", env=contributor_env)

            roygbiv_env = dict(public_env)
            roygbiv_env.update(
                {
                    "GIT_AUTHOR_NAME": "usr-bin-roygbiv",
                    "GIT_AUTHOR_EMAIL": "usr-bin-roygbiv@example.invalid",
                    "GIT_COMMITTER_NAME": "private control",
                    "GIT_COMMITTER_EMAIL": "private-control@example.invalid",
                }
            )
            (control / "product.txt").write_text("roygbiv\n", encoding="utf-8")
            self.git(control, "commit", "-am", "roygbiv change", env=roygbiv_env)

            owner_env = dict(public_env)
            owner_env.update(
                {
                    "GIT_AUTHOR_NAME": "private control",
                    "GIT_AUTHOR_EMAIL": "private-control@example.invalid",
                    "GIT_COMMITTER_NAME": "private control",
                    "GIT_COMMITTER_EMAIL": "private-control@example.invalid",
                }
            )
            (control / "product.txt").write_text("owner\n", encoding="utf-8")
            self.git(control, "commit", "-am", "owner change", env=owner_env)
            self.git(
                control,
                "commit",
                "--allow-empty",
                "-m",
                "owner empty marker",
                env=owner_env,
            )
            source = self.git(control, "rev-parse", "HEAD")

            hook_dir = control / "private-hooks"
            hook_dir.mkdir()
            marker = root / "hook-ran"
            hook = hook_dir / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o755)
            self.git(control, "config", "core.hooksPath", str(hook_dir))
            self.git(control, "config", "user.name", "private control")
            self.git(control, "config", "user.email", "private-control@example.invalid")

            nightly.create_isolated_candidate(control, candidate, source)
            self.assertFalse(marker.exists())
            self.assertNotEqual(
                (control / self.git(control, "rev-parse", "--git-common-dir")).resolve(),
                (candidate / self.git(candidate, "rev-parse", "--git-common-dir")).resolve(),
            )
            self.assertNotIn("user.email", self.git(candidate, "config", "--local", "--list").lower())

            candidate_sha = nightly.rebase_candidate(candidate, baseline, baseline, source)
            self.assertFalse(marker.exists())
            metadata = self.git(
                candidate,
                "log",
                "--reverse",
                "--format=%an|%ae|%cn|%ce",
                f"{baseline}..{candidate_sha}",
            ).splitlines()
            self.assertEqual(
                [
                    "iarbpairs|235640537+iarbpairs@users.noreply.github.com|cmux integration|cmux-integration@users.noreply.github.com",
                    "usr-bin-roygbiv|282155169+usr-bin-roygbiv@users.noreply.github.com|cmux integration|cmux-integration@users.noreply.github.com",
                    "cmux integration|cmux-integration@users.noreply.github.com|cmux integration|cmux-integration@users.noreply.github.com",
                    "cmux integration|cmux-integration@users.noreply.github.com|cmux integration|cmux-integration@users.noreply.github.com",
                ],
                metadata,
            )
            self.assertEqual(
                [],
                nightly.scan_candidate_commit_metadata(
                    candidate,
                    baseline,
                    candidate_sha,
                    ("private control", "private-control@example.invalid"),
                ),
            )

    def test_candidate_fetches_public_source_absent_from_control_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            public = root / "public"
            control = root / "control"
            candidate = root / "candidate"
            public.mkdir()
            control.mkdir()
            identity = dict(os.environ)
            identity.update(
                {
                    "GIT_AUTHOR_NAME": "cmux integration",
                    "GIT_AUTHOR_EMAIL": "cmux-integration@users.noreply.github.com",
                    "GIT_COMMITTER_NAME": "cmux integration",
                    "GIT_COMMITTER_EMAIL": "cmux-integration@users.noreply.github.com",
                }
            )

            self.git(public, "init", "--initial-branch=main")
            (public / "product.txt").write_text("lease\n", encoding="utf-8")
            self.git(public, "add", "product.txt")
            self.git(public, "commit", "-m", "lease", env=identity)
            lease = self.git(public, "rev-parse", "HEAD")
            self.git(public, "branch", nightly.INTEGRATION_BRANCH, lease)
            (public / "product.txt").write_text("source\n", encoding="utf-8")
            self.git(public, "commit", "-am", "source", env=identity)
            source = self.git(public, "rev-parse", "HEAD")

            self.git(control, "init", "--initial-branch=main")
            (control / "private.txt").write_text("control\n", encoding="utf-8")
            self.git(control, "add", "private.txt")
            self.git(control, "commit", "-m", "control", env=identity)
            with self.assertRaises(subprocess.CalledProcessError):
                self.git(control, "cat-file", "-e", f"{source}^{{commit}}")

            nightly.create_isolated_candidate(
                control,
                candidate,
                source,
                public_remote=str(public),
                expected_lease=lease,
            )

            self.assertEqual(source, self.git(candidate, "rev-parse", "HEAD"))


    def test_candidate_deepens_public_source_from_a_shallow_control_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            public = root / "public"
            control = root / "control"
            candidate = root / "candidate"
            public.mkdir()
            identity = dict(os.environ)
            identity.update(
                {
                    "GIT_AUTHOR_NAME": "cmux integration",
                    "GIT_AUTHOR_EMAIL": "cmux-integration@users.noreply.github.com",
                    "GIT_COMMITTER_NAME": "cmux integration",
                    "GIT_COMMITTER_EMAIL": "cmux-integration@users.noreply.github.com",
                }
            )

            self.git(public, "init", "--initial-branch=main")
            (public / "product.txt").write_text("base\n", encoding="utf-8")
            self.git(public, "add", "product.txt")
            self.git(public, "commit", "-m", "base", env=identity)
            base = self.git(public, "rev-parse", "HEAD")
            self.git(public, "branch", nightly.INTEGRATION_BRANCH, base)
            (public / "product.txt").write_text("source\n", encoding="utf-8")
            self.git(public, "commit", "-am", "source", env=identity)
            source = self.git(public, "rev-parse", "HEAD")
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    "main",
                    public.resolve().as_uri(),
                    str(control),
                ],
                check=True,
            )

            nightly.create_isolated_candidate(
                control,
                candidate,
                source,
                public_remote=str(public),
                expected_lease=base,
            )

            self.assertEqual(base, self.git(candidate, "merge-base", source, base))


    def test_isolated_git_environment_discards_identity_and_config_overrides(self) -> None:
        poisoned = {
            "PATH": "/usr/bin",
            "HOME": "/private/home",
            "GIT_DIR": "/private/repository",
            "GIT_CONFIG_GLOBAL": "/private/config",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/private/hooks",
            "GIT_AUTHOR_NAME": "private control",
            "GIT_COMMITTER_EMAIL": "private-control@example.invalid",
            "EMAIL": "private-control@example.invalid",
        }
        isolated = nightly.isolated_git_environment(poisoned)
        self.assertEqual("/usr/bin", isolated["PATH"])
        self.assertEqual("/dev/null", isolated["GIT_CONFIG_GLOBAL"])
        self.assertEqual("1", isolated["GIT_CONFIG_NOSYSTEM"])
        for name in (
            "GIT_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_AUTHOR_NAME",
            "GIT_COMMITTER_EMAIL",
            "EMAIL",
        ):
            self.assertNotIn(name, isolated)

    def test_command_failures_report_a_redacted_output_tail(self) -> None:
        secret = "provider-secret-value"
        failure = subprocess.CalledProcessError(
            23,
            ["agent"],
            output="model request started\n",
            stderr=f"model request rejected for {secret}\n",
        )
        with patch.object(nightly.subprocess, "run", side_effect=failure):
            with self.assertRaises(nightly.NightlyError) as raised:
                nightly._run(
                    ["agent"],
                    cwd=ROOT,
                    env={"PI_GPT_PROXY_API_KEY": secret},
                    label="restricted OMP reconciliation",
                )
        message = str(raised.exception)
        self.assertIn("restricted OMP reconciliation failed with exit code 23", message)
        self.assertIn("model request rejected for [redacted]", message)
        self.assertNotIn(secret, message)

    def test_command_failure_tail_fits_single_woodpecker_log_entry(self) -> None:
        failure = subprocess.CalledProcessError(
            101,
            ["cargo", "test"],
            output="compiling dependency\\n" * 300,
            stderr="error: final actionable diagnostic\\n",
        )
        with patch.object(nightly.subprocess, "run", side_effect=failure):
            with self.assertRaises(nightly.NightlyError) as raised:
                nightly._run(["cargo", "test"], cwd=ROOT, label="Rust validation")
        message = str(raised.exception)
        self.assertLessEqual(len(message.encode("utf-8")), 900)
        self.assertIn("error: final actionable diagnostic", message)

    def test_command_failure_tail_preserves_stdout_and_stderr_diagnostics(self) -> None:
        failure = subprocess.CalledProcessError(
            101,
            ["cargo", "test"],
            output=("test output\\n" * 300) + "failures: exact test name\\n",
            stderr=("compiling crate\\n" * 300) + "compiler context\\n",
        )
        with patch.object(nightly.subprocess, "run", side_effect=failure):
            with self.assertRaises(nightly.NightlyError) as raised:
                nightly._run(["cargo", "test"], cwd=ROOT, label="Rust validation")
        message = str(raised.exception)
        self.assertLessEqual(len(message.encode("utf-8")), 900)
        self.assertIn("failures: exact test name", message)
        self.assertIn("compiler context", message)

    def test_command_failure_tail_preserves_source_state_context(self) -> None:
        failure = subprocess.CalledProcessError(
            101,
            ["cargo", "test"],
            output=(
                ("test output\n" * 300)
                + "assertion failed: an unchanged source tree changed build identity\n"
                + "before:\n?? cmux-tui/new-source.rs\n"
                + "after:\n?? cmux-tui/generated-source.rs\n"
                + f"left: {'a' * 120}\n"
                + f"right: {'b' * 120}\n"
                + "note: run with RUST_BACKTRACE=1\n"
                + "failures: incremental source identity\n"
            ),
            stderr="error: test failed\n",
        )
        with patch.object(nightly.subprocess, "run", side_effect=failure):
            with self.assertRaises(nightly.NightlyError) as raised:
                nightly._run(["cargo", "test"], cwd=ROOT, label="Rust validation")
        message = str(raised.exception)
        self.assertLessEqual(len(message.encode("utf-8")), 900)
        self.assertIn("before:", message)
        self.assertIn("?? cmux-tui/new-source.rs", message)
        self.assertIn("after:", message)
        self.assertIn("?? cmux-tui/generated-source.rs", message)


class PublicImageAndOmpIsolationTests(unittest.TestCase):
    def test_ci_image_uses_digest_pinned_public_upstreams(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        image = (ROOT / "ci" / "rmux-ci.Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("docker.io", pipeline + image)
        self.assertNotIn("oven/bun", pipeline + image)
        self.assertIn("public.ecr.aws/docker/library/python:3.12.11-slim-bookworm@sha256:", image)
        self.assertIn("public.ecr.aws/docker/library/rust:1.95.0-bookworm@sha256:", image)
        self.assertIn("public.ecr.aws/docker/library/node:22.14.0-bookworm-slim@sha256:", image)
        self.assertNotIn("public.ecr.aws/docker/library/debian:", image)
        self.assertIn("BUN_VERSION=1.3.14", image)
        self.assertIn("951ee2aee855f08595aeec6225226a298d3fea83a3dcd6465c09cbccdf7e848f", image)
        self.assertIn("a27ffb63a8310375836e0d6f668ae17fa8d8d18b88c37c821c65331973a19a3b", image)
        self.assertIn("sha256sum --check", image)
        nightly_block = pipeline.split("reconcile-candidate:", 1)[1].split("  privacy-candidate:", 1)[0]
        self.assertIn("${RMUX_CI_PULL_REGISTRY}/linkedin-bot/rmux-ci:sha-${CI_COMMIT_SHA}", nightly_block)
        self.assertNotIn("rustup.rs", pipeline + image)
        self.assertNotIn("rustup.sh", pipeline + image)

    def test_multistage_cache_image_replaces_runtime_bootstrap(self) -> None:
        dockerfile = ROOT / "ci" / "rmux-ci.Dockerfile"
        self.assertTrue(dockerfile.is_file())
        image = dockerfile.read_text(encoding="utf-8")
        self.assertIn(" AS node-runtime", image)
        self.assertIn(" AS python-runtime", image)
        self.assertIn(" AS rust-planner", image)
        self.assertIn(" AS rust-cache", image)
        self.assertIn(" AS rust-deps", image)
        self.assertIn(" AS ci-runtime", image)
        self.assertIn("cargo chef prepare", image)
        self.assertIn("COPY cmux-tui/vendor/ ./vendor/", image)
        self.assertIn("cargo chef cook", image)
        self.assertIn("cargo test --workspace --locked --no-run", image)
        self.assertIn("ARG RMUX_BUILD_COMMIT", image)
        self.assertIn("CMUX_TUI_BUILD_COMMIT=\"$RMUX_BUILD_COMMIT\"", image)
        self.assertIn("CARGO_PROFILE_TEST_DEBUG=0", image)
        self.assertIn("CARGO_INCREMENTAL=0", image)
        self.assertIn("ZIG_GLOBAL_CACHE_DIR=/opt/zig-global-cache", image)
        self.assertIn("zig build --fetch", image)
        self.assertIn("COPY --chown=65532:65532 --from=rust-cache /opt/rmux-target/", image)
        self.assertIn("COPY --chown=65532:65532 --from=rust-cache /opt/npm-cache/", image)
        self.assertIn("WORKDIR /opt/rmux-source/cmux-tui", image)
        self.assertIn("/opt/rmux-source-tree", image)
        self.assertIn(
            "COPY --chown=65532:65532 --from=rust-cache /opt/rmux-source/ /opt/rmux-source/",
            image,
        )
        self.assertIn("COPY --chown=65532:65532 ghostty/ /opt/ghostty/", image)
        self.assertNotIn("RUN chown -R 65532:65532 /opt/ghostty", image)
        self.assertIn("RUN find /opt/rmux-source -type d -exec chmod 0755 {} +", image)

        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        clone = pipeline.split("steps:\n", 1)[0]
        self.assertIn("clone:\n", clone)
        self.assertIn(
            "quay.io/woodpeckerci/plugin-git:2.6.5@sha256:"
            "2b34ecbbfd2f318405bd5d2765a750f6b02dae5dfb8707e16c943b9faeef5fc5",
            clone,
        )
        self.assertIn("lfs: false", clone)
        self.assertIn("recursive: false", clone)
        self.assertIn("depth: 1", clone)
        self.assertIn("partial: false", clone)
        self.assertIn('git rev-parse HEAD:cmux-tui', pipeline)
        self.assertIn("--opt build-arg:RMUX_BUILD_COMMIT=$RMUX_SOURCE_TREE", pipeline)
        source = (ROOT / "scripts/integration/nightly.py").read_text(encoding="utf-8")
        self.assertIn('["rev-parse", "HEAD:cmux-tui"], "source tree revision"', source)
        self.assertIn('Path("/opt/rmux-source")', source)
        self.assertIn('Path("/opt/rmux-source-tree")', source)
        self.assertIn('"CMUX_GHOSTTY_SRC": "/opt/ghostty"', source)
        self.assertIn("chown_sandbox=False", source)
        self.assertIn("remove_sandbox=False", source)
        self.assertIn("--opt target=rust-deps", pipeline)
        self.assertIn("rmux-ci:deps-cache", pipeline)
        self.assertIn("rmux-ci:deps-buildcache", pipeline)
        self.assertIn("rmux-ci:source-buildcache", pipeline)
        self.assertNotIn('CACHE_REF="${RMUX_CI_REGISTRY}/linkedin-bot/rmux-ci:buildcache"', pipeline)
        self.assertNotIn("--opt target=rust-cache", pipeline)
        self.assertEqual(2, pipeline.count('buildctl --addr "tcp://${RMUX_BUILDKIT_HOST}:1234"'))
        self.assertIn("compression=zstd", pipeline)
        self.assertIn("RMUX_CI_PULL_REGISTRY", pipeline)
        self.assertIn("${RMUX_CI_REGISTRY}/linkedin-bot/rmux-ci:sha-${CI_COMMIT_SHA}", pipeline)
        self.assertNotIn("apt-get update", pipeline)
        self.assertIn("  - event: pull_request", pipeline)
        steps = pipeline.split("steps:\n", 1)[1].rsplit("\nwhen:\n", 1)[0]
        step_blocks = {
            match.group("name"): match.group(0)
            for match in re.finditer(
                r"(?ms)^  (?P<name>[a-z0-9-]+):\n.*?(?=^  [a-z0-9-]+:\n|\Z)",
                steps,
            )
        }
        self.assertIn("hydrate-ghostty-source", step_blocks)
        hydrate = step_blocks["hydrate-ghostty-source"]
        self.assertIn(
            "git submodule update --init --depth 1 --filter=blob:none ghostty",
            hydrate,
        )
        self.assertIn("GIT_LFS_SKIP_SMUDGE=1", hydrate)
        self.assertNotIn("--recursive", hydrate)
        self.assertNotIn("homebrew-cmux", hydrate)
        self.assertNotIn("vendor/bonsplit", hydrate)
        self.assertIn(
            "depends_on: [hydrate-ghostty-source]",
            step_blocks["build-ci-image"],
        )
        pr_steps = {"pr-privacy-source", "pr-cache-image-contract"}
        self.assertEqual(
            pr_steps,
            {name for name in step_blocks if name.startswith("pr-")},
        )
        for name, block in step_blocks.items():
            if name in pr_steps:
                self.assertIn("pull-request-events", block)
                self.assertNotIn("from_secret:", block)
            else:
                self.assertIn("nightly-events", block)
                self.assertNotIn("event: pull_request", block)
        self.assertNotIn(
            "--require-runtime-denylist",
            step_blocks["pr-privacy-source"],
        )
        self.assertNotIn("./scripts/install-zig-ci.sh", pipeline)
        self.assertNotIn("bun-v1.3.14", pipeline)

    def test_omp_environment_is_minimal_and_registry_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_env = {
                "PATH": "/usr/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "GPT_PROXY_BASE_URL": "http://gateway.example.test/v1",
                "PI_GPT_PROXY_API_KEY": "synthetic-gateway-token",
                "GITHUB_TOKEN": "must-not-pass",
                "GH_TOKEN": "must-not-pass",
                "CI_JOB_ID": "must-not-pass",
            }
            with patch.dict(os.environ, source_env, clear=True):
                environment, home = nightly._safe_agent_environment(root)
            try:
                self.assertEqual(
                    {
                        "HOME",
                        "PATH",
                        "LANG",
                        "LC_ALL",
                        "NO_COLOR",
                        "PI_CONFIG_DIR",
                        "PI_CODING_AGENT_DIR",
                        "PI_GPT_PROXY_API_KEY",
                    },
                    set(environment),
                )
                self.assertNotIn("GPT_PROXY_BASE_URL", environment)
                self.assertNotIn("GITHUB_TOKEN", environment)
                self.assertNotIn("GH_TOKEN", environment)
                self.assertFalse(any(name.startswith("CI_") for name in environment))
                agent_dir = Path(environment["PI_CODING_AGENT_DIR"])
                models = json.loads((agent_dir / "models.yml").read_text(encoding="utf-8"))
                config = json.loads((agent_dir / "config.yml").read_text(encoding="utf-8"))
                provider = models["providers"]["gpt-proxy"]
                model = provider["models"][0]
                self.assertEqual("http://gateway.example.test/v1", provider["baseUrl"])
                self.assertEqual("openai-responses", provider["api"])
                self.assertIs(provider["authHeader"], True)
                self.assertEqual("PI_GPT_PROXY_API_KEY", provider["apiKey"])
                self.assertEqual({"supportsDeveloperRole": True}, provider["compat"])
                self.assertEqual(
                    {
                        "id": "gpt-5.6-sol",
                        "name": "GPT 5.6 Sol Proxy",
                        "reasoning": True,
                        "input": ["text", "image"],
                        "contextWindow": 372000,
                        "maxTokens": 128000,
                        "preferWebsockets": False,
                        "thinking": {"mode": "effort", "minLevel": "low", "maxLevel": "xhigh"},
                    },
                    model,
                )
                self.assertEqual({"modelRoles": {"default": "gpt-proxy/gpt-5.6-sol"}}, config)
            finally:
                import shutil

                shutil.rmtree(home, ignore_errors=True)

    def test_omp_proxy_url_rejects_userinfo_even_over_http(self) -> None:
        source_env = {
            "PATH": "/usr/bin",
            "GPT_PROXY_BASE_URL": "http://synthetic-user" + "@" + "gateway.example.test/v1",
            "PI_GPT_PROXY_API_KEY": "synthetic-gateway-token",
        }
        with patch.dict(os.environ, source_env, clear=True):
            with self.assertRaises(nightly.NightlyError):
                nightly._safe_agent_environment(Path("."))

    def test_woodpecker_supplies_only_provider_runtime_secrets_to_omp(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        nightly_block = pipeline.split("reconcile-candidate:", 1)[1].split("  privacy-candidate:", 1)[0]
        self.assertIn("GPT_PROXY_BASE_URL", nightly_block)
        self.assertIn("from_secret: GPT_PROXY_BASE_URL", nightly_block)
        self.assertIn("PI_GPT_PROXY_API_KEY", nightly_block)
        for forbidden in ("GITHUB_TOKEN", "CMUX_FORK_PUSH_TOKEN", "CMUX_PRIVACY_DENYLIST_JSON", "CI_" + "REPO"):
            self.assertNotIn(forbidden, nightly_block)

    def test_nightly_pipeline_contains_no_personal_identifiers_or_hostnames(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8").lower()
        for forbidden in ("z-" + "peterson", "/home/" + "zac", "davai" + "local", "tailb" + "18de3", ".svc." + "cluster.local"):
            self.assertNotIn(forbidden, pipeline)

    def test_rust_validation_bounds_internal_test_parallelism(self) -> None:
        with patch.object(nightly, "_validate_candidate_in_sandbox") as validate:
            nightly.validate_candidate_rust(Path("/control"), Path("/pair"), 65532)

        commands = validate.call_args.args[2]
        self.assertEqual(
            ["cargo", "test", "--workspace", "--locked", "--", "--test-threads=4"],
            commands[0][0],
        )


class NativeCronPipelineTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, document: dict[str, object], status: int = 201) -> None:
            self._body = json.dumps(document).encode("utf-8")
            self.status = status

        def __enter__(self) -> "NativeCronPipelineTests.FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    @staticmethod
    def _step(pipeline: str, name: str, next_name: str | None) -> str:
        block = pipeline.split(f"  {name}:\n", 1)[1]
        return block if next_name is None else block.split(f"  {next_name}:\n", 1)[0]

    def test_pipeline_is_one_native_cron_dependency_chain(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        self.assertNotIn("WOODPECKER_" + "TRIGGER", pipeline)
        self.assertNotIn("CMUX_NIGHTLY_" + "FINALIZE", pipeline)
        self.assertIn("event: " + "manual", pipeline)
        self.assertNotIn("event: " + "push", pipeline)
        self.assertNotIn("CI_" + "REPO", pipeline)
        self.assertIn("event: cron", pipeline)
        steps = (
            "privacy-source",
            "cache-image-contract",
            "hydrate-ghostty-source",
            "build-ci-image",
            "preflight-rust",
            "preflight-node",
            "preflight-python",
            "prepare-candidate",
            "reconcile-candidate",
            "privacy-candidate",
            "validate-candidate-rust",
            "validate-candidate-node",
            "validate-candidate-python",
            "push-candidate",
            "render-nightly-report",
            "push-nightly-report",
            "publish-report-status",
        )
        dependencies = {
            "cache-image-contract": "[privacy-source]",
            "hydrate-ghostty-source": "[cache-image-contract]",
            "build-ci-image": "[hydrate-ghostty-source]",
            "preflight-rust": "[build-ci-image]",
            "preflight-node": "[build-ci-image]",
            "preflight-python": "[build-ci-image]",
            "prepare-candidate": "[preflight-rust, preflight-node, preflight-python]",
            "reconcile-candidate": "[prepare-candidate]",
            "privacy-candidate": "[reconcile-candidate]",
            "validate-candidate-rust": "[privacy-candidate]",
            "validate-candidate-node": "[privacy-candidate]",
            "validate-candidate-python": "[privacy-candidate]",
            "push-candidate": "[validate-candidate-rust, validate-candidate-node, validate-candidate-python]",
            "render-nightly-report": "[push-candidate]",
            "push-nightly-report": "[render-nightly-report]",
            "publish-report-status": "[push-nightly-report]",
        }
        for index, step in enumerate(steps):
            next_step = steps[index + 1] if index + 1 < len(steps) else None
            block = self._step(pipeline, step, next_step)
            if step in dependencies:
                self.assertIn(f"depends_on: {dependencies[step]}", block)

    def test_prepare_rebases_the_trigger_commit_instead_of_a_stale_public_lease(self) -> None:
        for pipeline_name in (".woodpecker.yml", ".woodpecker-control.yml"):
            pipeline = (ROOT / pipeline_name).read_text(encoding="utf-8")
            block = self._step(pipeline, "prepare-candidate", "reconcile-candidate")
            self.assertIn("--source-sha ${CI_COMMIT_SHA}", block)


    def test_native_steps_split_credentials_by_capability(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        ordered = (
            "privacy-source",
            "cache-image-contract",
            "build-ci-image",
            "preflight-rust",
            "preflight-node",
            "preflight-python",
            "prepare-candidate",
            "reconcile-candidate",
            "privacy-candidate",
            "validate-candidate-rust",
            "validate-candidate-node",
            "validate-candidate-python",
            "push-candidate",
            "render-nightly-report",
            "push-nightly-report",
            "publish-report-status",
        )
        blocks = {
            name: self._step(pipeline, name, ordered[index + 1] if index + 1 < len(ordered) else None)
            for index, name in enumerate(ordered)
        }
        self.assertIn("CMUX_PRIVACY_DENYLIST_JSON", blocks["privacy-source"])
        self.assertIn("CMUX_PRIVACY_DENYLIST_JSON", blocks["privacy-candidate"])
        self.assertIn("CMUX_PRIVACY_DENYLIST_JSON", blocks["render-nightly-report"])
        self.assertIn("CMUX_FORK_PUSH_TOKEN", blocks["push-candidate"])
        self.assertIn("CMUX_FORK_PUSH_TOKEN", blocks["push-nightly-report"])
        self.assertIn("GITHUB_TOKEN", blocks["publish-report-status"])
        self.assertIn("RMUX_REGISTRY_AUTH_JSON", blocks["build-ci-image"])
        for name, block in blocks.items():
            allowed = {
                "privacy-source": {"CMUX_PRIVACY_DENYLIST_JSON"},
                "build-ci-image": {"RMUX_REGISTRY_AUTH_JSON"},
                "privacy-candidate": {"CMUX_PRIVACY_DENYLIST_JSON"},
                "render-nightly-report": {"CMUX_PRIVACY_DENYLIST_JSON"},
                "reconcile-candidate": {"GPT_PROXY_BASE_URL", "PI_GPT_PROXY_API_KEY"},
                "push-candidate": {"CMUX_FORK_PUSH_TOKEN"},
                "push-nightly-report": {"CMUX_FORK_PUSH_TOKEN"},
                "publish-report-status": {"GITHUB_TOKEN"},
            }.get(name, set())
            for credential in (
                "CMUX_PRIVACY_DENYLIST_JSON",
                "GPT_PROXY_BASE_URL",
                "PI_GPT_PROXY_API_KEY",
                "CMUX_FORK_PUSH_TOKEN",
                "GITHUB_TOKEN",
                "RMUX_REGISTRY_AUTH_JSON",
            ):
                self.assertEqual(credential in allowed, credential in block, f"{name}: {credential}")

    def test_prepared_pair_is_shared_and_upstream_is_not_recomputed(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        report = self._step(pipeline, "render-nightly-report", "push-nightly-report")
        self.assertIn(".integration-results/nightly-pair.json", pipeline)
        self.assertIn("--pair-state", report)
        self.assertNotIn("fetch", report)
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "pair.json"
            nightly.write_pair_state(
                state,
                "a" * 40,
                "b" * 40,
                "c" * 40,
                control_sha="e" * 40,
                control_hashes={"trusted": "f" * 64},
            )
            pair = nightly.load_pair_state(state)
            self.assertEqual(("a" * 40, "b" * 40), (pair["baseline_sha"], pair["candidate_sha"]))
            with self.assertRaises(nightly.NightlyError):
                nightly.require_pair(pair, baseline_sha="a" * 40, candidate_sha="d" * 40)

    def test_pair_state_allows_a_fully_upstreamed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "pair.json"
            nightly.write_pair_state(
                state,
                "a" * 40,
                "a" * 40,
                "b" * 40,
                control_sha="c" * 40,
                control_hashes={"trusted": "d" * 64},
            )

            pair = nightly.load_pair_state(state)

        self.assertEqual(pair["baseline_sha"], pair["candidate_sha"])

    def test_native_dispatch_correlates_the_new_exact_sha_run_after_empty_response(self) -> None:
        candidate = "b" * 40
        old_run = {"id": 7, "event": "workflow_dispatch", "head_sha": candidate, "head_branch": "nightly-integration"}
        new_run = {"id": 9, "event": "workflow_dispatch", "head_sha": candidate, "head_branch": "nightly-integration"}
        responses = iter(
            (
                json.dumps({"workflow_runs": [old_run]}).encode("utf-8"),
                b"",
                json.dumps({"workflow_runs": [new_run, old_run]}).encode("utf-8"),
            )
        )
        requests: list[tuple[str, str]] = []
        client = github_workflow_gate.GitHubClient("usr-bin-roygbiv/rmux", "synthetic-token")

        def request(method: str, path: str, payload: object = None) -> bytes:
            del payload
            requests.append((method, path))
            return next(responses)

        client._request = request  # type: ignore[method-assign]
        run_id = client.dispatch(
            "ci.yml",
            "nightly-integration",
            {"ref": candidate},
            poll_seconds=0,
            attempts=2,
        )

        self.assertEqual(9, run_id)
        self.assertEqual(["GET", "POST", "GET"], [method for method, _ in requests])


    def test_native_dispatch_correlates_custom_candidate_input(self) -> None:
        candidate = "c" * 40
        responses = iter(
            (
                json.dumps({"workflow_runs": []}).encode("utf-8"),
                b"",
                json.dumps(
                    {
                        "workflow_runs": [
                            {
                                "id": 11,
                                "event": "workflow_dispatch",
                                "head_sha": candidate,
                                "head_branch": "nightly-integration",
                            }
                        ]
                    }
                ).encode("utf-8"),
            )
        )
        client = github_workflow_gate.GitHubClient(
            "usr-bin-roygbiv/rmux", "synthetic-token"
        )
        client._request = lambda method, path, payload=None: next(responses)  # type: ignore[method-assign]

        run_id = client.dispatch(
            "cmux-tui.yml",
            "nightly-integration",
            {"commit": candidate, "mode": "full", "request_id": candidate},
            candidate_input="commit",
            poll_seconds=0,
            attempts=2,
        )

        self.assertEqual(11, run_id)

    def test_omp_uses_private_default_boundary_and_syncs_only_reviewed_product_files(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        block = self._step(pipeline, "reconcile-candidate", "privacy-candidate")
        control_pipeline = (ROOT / ".woodpecker-control.yml").read_text(encoding="utf-8")
        control_block = self._step(control_pipeline, "reconcile-candidate", "privacy-candidate")
        self.assertIn("backend_options:", pipeline)
        self.assertIn("serviceAccountName: woodpecker-ci-untrusted", pipeline)
        self.assertIn("kuber" + "netes:", pipeline)
        for prompt_block in (block, control_block):
            self.assertIn("--agent-uid 65532", prompt_block)
            self.assertIn("--tools=read,write,edit,grep,find", prompt_block)
            self.assertIn("Reconcile the prepared aggregate", prompt_block)
            self.assertIn(
                "Edit only existing paths listed in integration/nightly/product-files.txt.",
                prompt_block,
            )
            self.assertIn("Do not create, delete, or rename files.", prompt_block)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate"
            sandbox = root / "sandbox"
            (candidate / "Sources").mkdir(parents=True)
            (sandbox / "Sources").mkdir(parents=True)
            (candidate / ".github").mkdir()
            (sandbox / ".github").mkdir()
            (candidate / "Sources/Product.swift").write_text("before", encoding="utf-8")
            (sandbox / "Sources/Product.swift").write_text("after", encoding="utf-8")
            (candidate / ".github/workflow.yml").write_text("protected", encoding="utf-8")
            (sandbox / ".github/workflow.yml").write_text("modified", encoding="utf-8")
            with self.assertRaises(nightly.NightlyError):
                nightly.sync_reviewed_product_changes(
                    candidate,
                    sandbox,
                    [Path("Sources/Product.swift")],
                )
            self.assertEqual("before", (candidate / "Sources/Product.swift").read_text(encoding="utf-8"))
            (sandbox / ".github/workflow.yml").write_text("protected", encoding="utf-8")
            nightly.sync_reviewed_product_changes(candidate, sandbox, [Path("Sources/Product.swift")])
            self.assertEqual("after", (candidate / "Sources/Product.swift").read_text(encoding="utf-8"))
        reviewed = privacy_gate.paths_from_manifest(
            ROOT,
            ROOT / "integration/nightly/product-files.txt",
        )
        self.assertIn(Path("ci/rmux-ci.Dockerfile"), reviewed)
        self.assertIn(Path("cmux-tui/crates/cmux-tui-core/src/server.rs"), reviewed)
        self.assertIn(
            Path("cmux-tui/crates/cmux-tui-core/src/workspace_registry/terminal_exit_store.rs"),
            reviewed,
        )
        self.assertNotIn(Path(".woodpecker-control.yml"), reviewed)

    def test_candidate_status_command_preserves_leading_index_space(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout=" M cmux-tui/bindings/RELEASING.md\0",
            stderr="",
        )
        with patch.object(subprocess, "run", return_value=completed):
            status = nightly._run(
                ["git", "status"],
                cwd=ROOT,
                label="candidate status",
                strip_output=False,
            )

        self.assertEqual(" M cmux-tui/bindings/RELEASING.md\0", status)
        self.assertEqual(
            {Path("cmux-tui/bindings/RELEASING.md")},
            nightly._changed_paths_from_porcelain(status),
        )

    def test_candidate_status_parses_nul_paths_without_quote_or_rename_ambiguity(self) -> None:
        status = (
            " M Sources/Browser Panel.swift\0"
            "?? Sources/ümlaut.swift\0"
            " M ghostty\0"
        )
        self.assertEqual(
            {
                Path("Sources/Browser Panel.swift"),
                Path("Sources/ümlaut.swift"),
            },
            nightly._changed_paths_from_porcelain(status),
        )

    def test_product_manifest_covers_every_non_automation_change(self) -> None:
        changed = {
            Path(".woodpecker.yml"),
            Path(".woodpecker-control.yml"),
            Path("Sources/TerminalController.swift"),
            Path("cmux-tui/bindings/java/src/com/cmux/NotificationEvent.java"),
            Path("scripts/integration/nightly.py"),
            Path("tests/test_nightly_ci_hardening.py"),
        }
        with self.assertRaises(nightly.NightlyError):
            nightly.require_product_manifest_coverage(
                changed,
                [Path("Sources/TerminalController.swift")],
            )
        nightly.require_product_manifest_coverage(
            changed,
            [
                Path("Sources/TerminalController.swift"),
                Path("cmux-tui/bindings/java/src/com/cmux/NotificationEvent.java"),
            ],
        )

    def test_product_manifest_review_excludes_non_file_gitlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product = Path("Sources/TerminalController.swift")
            gitlink = Path("vendor/bonsplit")
            (root / product).parent.mkdir(parents=True)
            (root / product).write_text("product", encoding="utf-8")
            (root / gitlink).mkdir(parents=True)

            self.assertEqual(
                [product],
                nightly.reviewable_changed_paths(root, [gitlink, product]),
            )

    def test_source_privacy_scans_only_control_plane_manifest(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        source_block = self._step(pipeline, "privacy-source", "preflight-rust")
        public_manifest = Path("integration/nightly/public-files.txt")
        self.assertIn(f"--manifest {public_manifest.as_posix()}", source_block)
        self.assertNotIn("--manifest integration/nightly/product-files.txt", source_block)
        paths = privacy_gate.paths_from_manifest(ROOT, ROOT / public_manifest)
        self.assertNotIn(Path(".woodpecker.yml"), paths)
        self.assertEqual([], privacy_gate.scan_paths(ROOT, paths))

    def test_rust_steps_use_preverified_zig_and_ghostty_cache(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        image = (ROOT / "ci" / "rmux-ci.Dockerfile").read_text(encoding="utf-8")
        preflight = self._step(pipeline, "preflight-rust", "preflight-node")
        self.assertIn("test -x /usr/local/bin/zig", preflight)
        self.assertIn("test -s /opt/ghostty/src/unicode/lut.zig", preflight)
        self.assertIn("nightly.py preflight-rust --agent-uid 65532", preflight)
        self.assertNotIn("cd cmux-tui && cargo test", preflight)
        self.assertIn("ZIG_FORCE_LOCAL_INSTALL=1", image)
        self.assertIn("CMUX_GHOSTTY_SRC=/opt/ghostty", image)
        self.assertNotIn("./scripts/install-zig-ci.sh", pipeline)

    def test_post_omp_validation_is_split_into_unprivileged_ephemeral_copies(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        rust = self._step(pipeline, "validate-candidate-rust", "validate-candidate-node")
        node = self._step(pipeline, "validate-candidate-node", "validate-candidate-python")
        python = self._step(pipeline, "validate-candidate-python", "push-candidate")
        cached_image = "${RMUX_CI_PULL_REGISTRY}/linkedin-bot/rmux-ci:sha-${CI_COMMIT_SHA}"
        self.assertIn(cached_image, rust)
        self.assertIn("nightly.py validate-rust", rust)
        self.assertIn(cached_image, node)
        self.assertIn("nightly.py validate-node", node)
        self.assertIn(cached_image, python)
        self.assertIn("nightly.py validate-python", python)
        for block in (rust, node, python):
            self.assertIn("--agent-uid 65532", block)
        self.assertNotIn(".integration-results/candidate/scripts/", pipeline)

    @unittest.skipUnless(os.geteuid() == 0, "requires the production root boundary")
    def test_malicious_candidate_cannot_touch_or_execute_trusted_control_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            control = Path(temp) / "control"
            script = control / "scripts/integration/nightly.py"
            pair = control / ".integration-results/nightly-pair.json"
            config = control / ".git/config"
            hook = control / ".git/hooks/pre-push"
            for path, content in (
                (script, "#!/bin/sh\nprintf executed > \"$1\"\n"),
                (pair, "trusted-pair"),
                (config, "trusted-config"),
                (hook, "trusted-hook"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            script.chmod(0o755)
            sandbox = Path(tempfile.mkdtemp(prefix="cmux-malicious-candidate-"))
            home = Path(tempfile.mkdtemp(prefix="cmux-malicious-home-"))
            marker = Path(tempfile.gettempdir()) / f"cmux-control-executed-{os.getpid()}"
            marker.unlink(missing_ok=True)
            attack = """
import os
from pathlib import Path
import subprocess
import sys
workspace = Path(sys.argv[1])
marker = Path(sys.argv[2])
if os.getuid() != 65532:
    raise SystemExit("candidate did not run as the unprivileged uid")
breaches = []
for name in ("GITHUB_TOKEN", "CMUX_FORK_PUSH_TOKEN", "CMUX_PRIVACY_DENYLIST_JSON", "PI_GPT_PROXY_API_KEY"):
    if name in os.environ:
        breaches.append("secret-env:" + name)
for target in (
    workspace / "scripts/integration/nightly.py",
    workspace / ".integration-results/nightly-pair.json",
    workspace / ".git/config",
    workspace / ".git/hooks/pre-push",
):
    try:
        target.write_text("owned", encoding="utf-8")
        breaches.append(str(target))
    except OSError:
        pass
try:
    subprocess.run([str(workspace / "scripts/integration/nightly.py"), str(marker)], check=False)
    if marker.exists():
        breaches.append("executed-control")
except OSError:
    pass
if breaches:
    raise SystemExit("candidate breached trusted control: " + ", ".join(breaches))
"""
            nightly.run_isolated_commands(
                control,
                sandbox,
                home,
                [([sys.executable, "-c", attack, str(control), str(marker)], Path("."))],
                agent_uid=65532,
            )
            self.assertEqual('#!/bin/sh\nprintf executed > "$1"\n', script.read_text(encoding="utf-8"))
            self.assertEqual("trusted-pair", pair.read_text(encoding="utf-8"))
            self.assertEqual("trusted-config", config.read_text(encoding="utf-8"))
            self.assertEqual("trusted-hook", hook.read_text(encoding="utf-8"))
            self.assertFalse(marker.exists())
            self.assertFalse(sandbox.exists())
            self.assertFalse(home.exists())

    def test_pair_and_trusted_control_hashes_fail_closed_before_credentials(self) -> None:
        pair = {
            "control_sha": "a" * 40,
            "candidate_sha": "b" * 40,
            "control_hashes": {"trusted": "c" * 64},
            "validated_rust_sha": "b" * 40,
            "validated_node_sha": "b" * 40,
            "validated_python_sha": "b" * 40,
        }
        nightly.require_trusted_attestation(
            pair,
            control_sha="a" * 40,
            head_sha="b" * 40,
            head_key="candidate_sha",
            control_hashes={"trusted": "c" * 64},
            required_markers=("validated_rust_sha", "validated_node_sha", "validated_python_sha"),
        )
        with self.assertRaises(nightly.NightlyError):
            nightly.require_trusted_attestation(
                pair,
                control_sha="a" * 40,
                head_sha="b" * 40,
                head_key="candidate_sha",
                control_hashes={"trusted": "d" * 64},
                required_markers=("validated_rust_sha",),
            )

    def test_python_commands_remain_rooted_and_perf_builds_use_exact_derived_data(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        preflight = self._step(pipeline, "preflight-python", "prepare-candidate")
        self.assertNotIn("cd cmux-tui/bindings/python", preflight)
        self.assertNotIn("apt-get", preflight)
        self.assertNotIn("pip install", preflight)
        self.assertIn(
            "PYTHONPATH=cmux-tui/bindings/python python3 -m unittest discover "
            "-s cmux-tui/bindings/python/tests",
            preflight,
        )
        self.assertNotIn("-t cmux-tui/bindings/python", preflight)
        self.assertNotIn("python3 -m pip install", preflight)
        self.assertIn("python3 -m pytest -q -p no:cacheprovider tests/test_perf_*.py", preflight)
        self.assertNotIn("unittest discover -s tests -p 'test_perf_*.py'", preflight)
        ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "python3 -m pytest -q -p no:cacheprovider tests/test_perf_*.py",
            ci_workflow,
        )
        self.assertNotIn(
            "unittest discover -s tests -p 'test_perf_*.py'",
            ci_workflow,
        )
        workflow = (ROOT / ".github/workflows/perf-activation.yml").read_text(encoding="utf-8")
        self.assertIn('./scripts/reload.sh --tag "$BASELINE_TAG" --derived-data "$BASELINE_DERIVED_DATA"', workflow)
        self.assertIn('./scripts/reload.sh --tag "$CANDIDATE_TAG" --derived-data "$CANDIDATE_DERIVED_DATA"', workflow)

    def test_report_status_retries_boundedly_on_the_exact_report_sha(self) -> None:
        report_sha = "d" * 40
        responses = [self.FakeResponse({}, 500), self.FakeResponse({"sha": report_sha})]
        delays: list[float] = []
        requests: list[object] = []

        def open_request(request: object, timeout: float) -> NativeCronPipelineTests.FakeResponse:
            del timeout
            requests.append(request)
            return responses.pop(0)

        published = nightly.publish_github_status(
            "usr-bin-roygbiv/rmux",
            report_sha,
            "synthetic-github-token",
            opener=open_request,
            sleep=delays.append,
            attempts=4,
        )
        self.assertEqual(report_sha, published)
        self.assertEqual([1.0], delays)
        self.assertEqual(2, len(requests))
        payload = json.loads(requests[-1].data)
        self.assertEqual(
            f"https://github.com/usr-bin-roygbiv/rmux/commit/{report_sha}/checks",
            payload["target_url"],
        )

    def test_public_target_never_comes_from_the_mirror_forge(self) -> None:
        pipeline = (ROOT / ".woodpecker.yml").read_text(encoding="utf-8")
        automation = (ROOT / "scripts/integration/nightly.py").read_text(encoding="utf-8")
        gate = (ROOT / "scripts/integration/github_workflow_gate.py").read_text(encoding="utf-8")
        mirror_repo_key = "CI_" + "REPO"
        for text in (pipeline, automation, gate):
            self.assertNotIn(mirror_repo_key, text)
            self.assertNotIn(mirror_repo_key + "_LINK", text)
            self.assertNotIn(mirror_repo_key + "_CLONE", text)
        manifest = json.loads((ROOT / "integration/nightly/workflows.json").read_text(encoding="utf-8"))
        self.assertEqual("usr-bin-roygbiv/rmux", manifest["repository"])

    def test_dispatched_workflows_are_parsed_as_secretless_read_only_jobs(self) -> None:
        _, specs = github_workflow_gate.load_workflow_manifest(ROOT / "integration/nightly/workflows.json")
        names = {spec.workflow for spec in specs}
        self.assertNotIn("ios-streamed-validate.yml", names)
        self.assertNotIn("test-e2e.yml", names)
        github_workflow_gate.audit_workflow_specs(ROOT, specs)

    def test_parsed_workflow_policy_rejects_secrets_tokens_and_write_permissions(self) -> None:
        fixtures = {
            "secret.yml": """
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@pinned
        with:
          persist-credentials: false
      - run: echo ok
        env:
          PRIVATE_VALUE: ${{ secrets.CUSTOM_VALUE }}
""",
            "github-context.yml": """
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@pinned
        with:
          persist-credentials: false
      - if: ${{ false }}
        run: echo unreachable
        env:
          GH_TOKEN: ${{ github.token }}
""",
            "github-input.yml": (
                "permissions:\n  contents: read\njobs:\n  test:\n    steps:\n"
                "      - uses: actions/checkout@pinned\n        with:\n"
                "          persist-credentials: false\n      - uses: public/action@pinned\n        with:\n"
                "          github-" + "token: synthetic\n"
            ),
            "credential-context.yml": """
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@pinned
        with:
          persist-credentials: false
      - run: echo unreachable
        env:
          FORK_CREDENTIAL: inherited
""",
            "persistent.yml": """
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@pinned
""",
            "write.yml": """
permissions:
  contents: read
  actions: write
jobs:
  test:
    steps:
      - uses: actions/checkout@pinned
        with:
          persist-credentials: false
""",
        }
        with tempfile.TemporaryDirectory() as temp:
            for name, text in fixtures.items():
                with self.subTest(name=name):
                    path = Path(temp) / name
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(github_workflow_gate.GateError):
                        github_workflow_gate.audit_candidate_workflow(path)


class PrivacyAndViewerHardeningTests(unittest.TestCase):
    def test_binary_and_non_utf8_tracked_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nul.bin").write_bytes(b"public\x00private")
            (root / "invalid.txt").write_bytes(b"\xff\xfe")
            findings = privacy_gate.scan_paths(root, [Path("nul.bin"), Path("invalid.txt")])
            self.assertEqual(
                [("invalid.txt", "binary"), ("nul.bin", "binary")],
                [(item.path.as_posix(), item.rule) for item in findings],
            )

    def test_new_binary_diff_fails_closed(self) -> None:
        diff = "\n".join(
            (
                "diff --git a/new.bin b/new.bin",
                "new file mode 100644",
                "index 0000000..1234567",
                "Binary files /dev/null and b/new.bin differ",
            )
        )
        findings = privacy_gate.scan_unified_diff(diff)
        self.assertEqual([(Path("new.bin"), 0, "binary")], [(item.path, item.line, item.rule) for item in findings])

    def test_real_public_manifest_is_self_clean(self) -> None:
        manifest_path = ROOT / "integration/nightly/public-files.txt"
        paths = privacy_gate.paths_from_manifest(ROOT, manifest_path)
        self.assertEqual([], privacy_gate.scan_paths(ROOT, paths))
    def test_static_public_viewer_instructions_do_not_require_woodpecker(self) -> None:
        for relative in ("integration/nightly/pull-request-body.md", "integration/nightly/report.md"):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("commit status", text.lower())
                self.assertIn("Checks", text)
                self.assertIn("Woodpecker", text)
                self.assertIn("private", text.lower())
                self.assertIn("not required", text.lower())


if __name__ == "__main__":
    unittest.main()
