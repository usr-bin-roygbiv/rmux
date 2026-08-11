#!/usr/bin/env python3
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PIPELINE = ROOT / ".woodpecker-control.yml"
INITIAL_PUBLIC_SEED = "2e1ad8cc39071e7f0c669e5d891b7a4779757b06"
INITIAL_PUBLIC_LEASE = "c3e16646fc077aa9a836befa1cd1fda9c48a8473"


class PrivateNativeControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = CONTROL_PIPELINE.read_text(encoding="utf-8")
        cls.dockerfile = (ROOT / "ci/rmux-ci.Dockerfile").read_text(encoding="utf-8")

    def step_block(self, name: str) -> str:
        match = re.search(
            rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z0-9][a-z0-9-]*:\n|^when:\n)",
            self.pipeline,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing private control step {name}")
        return match.group("body")

    def test_manual_and_permanent_native_cron_rebase_the_trigger_commit(self) -> None:
        prepare = self.step_block("prepare-candidate")
        self.assertIn(
            "python3 scripts/integration/nightly.py prepare --source-sha ${CI_COMMIT_SHA} "
            "--pair-state .integration-results/nightly-pair.json",
            prepare,
        )
        self.assertNotIn("--bootstrap-source-sha", prepare)
        self.assertRegex(
            self.pipeline,
            r"when:\n  - event: manual\n"
            r"  - event: cron\n    cron: nightly-integration-daily\n    branch: main\n?\Z",
        )
        for obsolete in (
            "WOODPECKER_TRIGGER_URL",
            "WOODPECKER_TRIGGER_TOKEN",
            "CMUX_NIGHTLY_FINALIZE",
            "/trigger",
            "scheduled_only_repos",
            "woodpecker-github-oauth",
        ):
            self.assertNotIn(obsolete, self.pipeline)

    def test_source_privacy_scans_only_control_plane_manifest(self) -> None:
        source = self.step_block("privacy-source")
        self.assertIn("--manifest integration/nightly/public-files.txt", source)
        self.assertNotIn("--manifest integration/nightly/product-files.txt", source)

    def test_reconcile_uses_bun_from_the_cached_ci_image(self) -> None:
        reconcile = self.step_block("reconcile-candidate")
        self.assertIn("bun x --bun @oh-my-pi/pi-coding-agent@17.1.6", reconcile)
        self.assertIn("bun-v${BUN_VERSION}/${asset}.zip", self.dockerfile)
        self.assertIn('install -m 0755 "/tmp/bun/${asset}/bun" /opt/bun/bun', self.dockerfile)
        self.assertNotIn("curl ", reconcile)

    def test_rust_steps_use_cached_ghostty_and_verified_zig(self) -> None:
        self.assertIn("COPY ghostty/ /opt/ghostty/", self.dockerfile)
        self.assertIn("RUNNER_TEMP=/opt/zig-cache /opt/rmux-scripts/install-zig-ci.sh", self.dockerfile)
        self.assertIn('ln -s "$ZIG" /usr/local/bin/zig', self.dockerfile)
        for step in ("preflight-rust", "validate-candidate-rust"):
            with self.subTest(step=step):
                block = self.step_block(step)
                self.assertNotIn("git submodule update", block)
                self.assertNotIn("install-zig-ci.sh", block)
                self.assertRegex(
                    block,
                    r"resources:\n\s+requests:\n\s+cpu: \"2\"\n\s+memory: 2Gi\n\s+limits:\n\s+cpu: \"4\"\n\s+memory: 6Gi",
                )
        self.assertIn(
            "test -x /usr/local/bin/zig && test -s /opt/ghostty/src/unicode/lut.zig",
            self.step_block("preflight-rust"),
        )

    def test_every_step_uses_the_flux_owned_tokenless_identity(self) -> None:
        steps = re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", self.pipeline, flags=re.MULTILINE)
        self.assertEqual(16, len(steps))
        for name in steps:
            with self.subTest(step=name):
                block = self.step_block(name)
                self.assertIn("backend_options:\n", block)
                self.assertIn("serviceAccountName: woodpecker-ci-untrusted", block)

    def test_credentials_remain_split_by_capability(self) -> None:
        expected = {
            "privacy-source": {"CMUX_PRIVACY_DENYLIST_JSON"},
            "reconcile-candidate": {"GPT_PROXY_BASE_URL", "PI_GPT_PROXY_API_KEY"},
            "privacy-candidate": {"CMUX_PRIVACY_DENYLIST_JSON"},
            "push-candidate": {"CMUX_FORK_PUSH_TOKEN"},
            "render-nightly-report": {"CMUX_PRIVACY_DENYLIST_JSON"},
            "push-nightly-report": {"CMUX_FORK_PUSH_TOKEN"},
            "publish-report-status": {"GITHUB_TOKEN"},
        }
        secret_names = {
            "CMUX_PRIVACY_DENYLIST_JSON",
            "GPT_PROXY_BASE_URL",
            "PI_GPT_PROXY_API_KEY",
            "CMUX_FORK_PUSH_TOKEN",
            "GITHUB_TOKEN",
        }
        steps = re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", self.pipeline, flags=re.MULTILINE)
        for name in steps:
            block = self.step_block(name)
            present = {secret for secret in secret_names if secret in block}
            self.assertEqual(expected.get(name, set()), present, name)
        self.assertIn("python3 tests/test_private_native_control.py", self.step_block("preflight-python"))
        reconcile = self.step_block("reconcile-candidate")
        self.assertIn("GPT_PROXY_BASE_URL", reconcile)
        self.assertIn("from_secret: GPT_PROXY_BASE_URL", reconcile)

    def test_python_binding_gate_is_importable_from_repo_root(self) -> None:
        preflight = self.step_block("preflight-python")
        self.assertIn("apt-get install -y -qq --no-install-recommends", self.dockerfile)
        self.assertIn(" git ", self.dockerfile)
        self.assertIn(
            "python3 -m pip install --disable-pip-version-check --no-cache-dir pytest==8.4.1",
            self.dockerfile,
        )
        self.assertNotIn("apt-get ", preflight)
        self.assertNotIn("pip install", preflight)
        self.assertIn(
            "PYTHONPATH=cmux-tui/bindings/python python3 -m unittest discover "
            "-s cmux-tui/bindings/python/tests",
            preflight,
        )
        self.assertNotIn("-t cmux-tui/bindings/python", preflight)
        self.assertIn("python3 -m pytest -q tests/test_perf_*.py", preflight)
        self.assertNotIn("unittest discover -s tests -p 'test_perf_*.py'", preflight)


if __name__ == "__main__":
    unittest.main()
