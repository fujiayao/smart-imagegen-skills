"""Release packaging checks: temporary installations, dummy data, no paid requests."""
from __future__ import annotations
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_module("release_install", PROJECT / "scripts" / "install.py")
scanner = load_module("release_check", PROJECT / "scripts" / "check_release.py")


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="imagegen-release-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def git(self, *args, cwd=None, input=None, check=True):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", *args], cwd=cwd or self.root, env=env,
                              input=input, capture_output=True, check=check)

    def init_git(self):
        self.git("init", "--initial-branch=main")
        shutil.copy2(PROJECT / ".gitignore", self.root / ".gitignore")


class InstallerTests(ReleaseCase):
    def test_self_contained_install_includes_licenses_and_pinned_vendor(self):
        target = installer.install(self.root / "imagegen-relay")
        for name in (*installer.PUBLIC_FILES, "LICENSE", "THIRD_PARTY_NOTICES.md"):
            self.assertTrue((target / name).is_file(), name)
        spec = json.loads((target / "references/components.json").read_text(encoding="utf-8"))["awakefantasy"]
        client = target / spec["root"] / spec["client_file"]
        self.assertEqual(hashlib.sha256(client.read_bytes()).hexdigest(), spec["sha256"])
        self.assertFalse((target / "tests").exists())

    def test_installed_dry_run_works_outside_checkout(self):
        target = installer.install(self.root / "imagegen-relay")
        output = self.root / "not-created.png"
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(target / "scripts/awake_engine.py"),
                                 "generate", "--model", "gpt-image-2", "--prompt", "offline fixture",
                                 "--out", str(output), "--dry-run"], cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertIn("Offline only", json.loads(result.stdout)["note"])
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".meta.json").exists())

    def test_existing_target_is_not_modified(self):
        target = self.root / "imagegen-relay"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exists"):
            installer.install(target)
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(list(target.iterdir()), [marker])

    def test_target_must_match_internal_id(self):
        with self.assertRaisesRegex(ValueError, "named imagegen-relay"):
            installer.install(self.root / "wrong-name")
        self.assertFalse((self.root / "wrong-name").exists())

    def test_default_install_refuses_existing_legacy_install(self):
        legacy = self.root / "codex" / "skills" / "imagegen-relay"
        legacy.mkdir(parents=True)
        with patch.object(Path, "home", return_value=self.root), patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")}):
            with self.assertRaisesRegex(ValueError, "already exists"):
                installer.install()
        self.assertTrue(legacy.is_dir())
        self.assertFalse((self.root / ".agents").exists())

    def test_allowlist_excludes_unexpected_nested_runtime_files(self):
        source = self.root / "source"
        shutil.copytree(SKILL, source)
        for name in (".secrets/fixture.dpapi", "scripts/private.key", "references/.cache/fixture.json", "scripts/.env", "scripts/extra.py"):
            file = source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("dummy fixture", encoding="utf-8")
        target = self.root / "imagegen-relay"
        with patch.object(installer, "SOURCE", source):
            installer.install(target)
        expected = set(installer.PUBLIC_FILES) | {"LICENSE", "THIRD_PARTY_NOTICES.md"}
        actual = {file.relative_to(target).as_posix() for file in target.rglob("*") if file.is_file()}
        self.assertEqual(actual, expected)

    def test_missing_source_fails_before_creating_target(self):
        with patch.object(installer, "SOURCE", self.root / "absent"):
            with self.assertRaisesRegex(ValueError, "absent"):
                installer.install(self.root / "imagegen-relay")
        self.assertFalse((self.root / "imagegen-relay").exists())


class IgnoreAndScannerTests(ReleaseCase):
    def test_gitignore_covers_sensitive_runtime_but_keeps_public_code(self):
        self.init_git()
        ignored = [".env", ".env.production", "skills/imagegen-relay/.secrets/key.dpapi",
                   "skills/imagegen-relay/.cache/prompts.json", "outputs/images/image.png",
                   "skills/imagegen-relay/auth.json", "config.toml", "config.local.json",
                   "fixture.local.toml", "fixture.pem", "cc-switch.db", "run.log", "fixture.meta.json"]
        public = ["README.md", ".env.example", "requirements.txt", "scripts/install.py",
                  "skills/imagegen-relay/references/components.json", "skills/imagegen-relay/tests/test_release.py"]
        for name in ignored + public:
            with self.subTest(path=name):
                result = self.git("check-ignore", "--no-index", "--", name, check=False)
                self.assertEqual(result.returncode, 0 if name in ignored else 1)

    def test_ignored_secrets_are_not_publication_candidates(self):
        self.init_git()
        (self.root / ".env").write_text("fixture only", encoding="utf-8")
        (self.root / "README.md").write_text("Public fixture", encoding="utf-8")
        with patch.object(scanner, "ROOT", self.root):
            files, findings = scanner.check()
        self.assertNotIn(self.root / ".env", files)
        self.assertIn(self.root / "README.md", files)
        self.assertEqual(findings, [])

    def test_force_staged_secret_is_detected_without_echoing_secret(self):
        self.init_git()
        secret = "sk" + "-" + "x" * 32
        (self.root / ".env").write_text("API_KEY=" + secret, encoding="utf-8")
        self.git("add", "-f", ".env")
        output = io.StringIO()
        with patch.object(scanner, "ROOT", self.root), contextlib.redirect_stdout(output):
            result = scanner.main()
        self.assertEqual(result, 1)
        self.assertIn("private runtime/configuration file", output.getvalue())
        self.assertIn("possible API key", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_personal_paths_and_email_are_flagged(self):
        self.init_git()
        personal = "C:" + "/" + "Users" + "/" + "fixture-user" + "/notes.txt"
        address = "fixture" + "@" + "example.invalid"
        (self.root / "notes.txt").write_text(personal + "\n" + address, encoding="utf-8")
        with patch.object(scanner, "ROOT", self.root):
            _, findings = scanner.check()
        self.assertEqual({label for _, _, label in findings}, {"personal absolute path", "personal email"})

    def test_binary_candidate_requires_review(self):
        self.init_git()
        (self.root / "fixture.bin").write_bytes(b"\xff\xfe")
        with patch.object(scanner, "ROOT", self.root):
            _, findings = scanner.check()
        self.assertIn("non-text file requires manual review", {label for _, _, label in findings})

    def test_scanner_fallback_works_without_git_repository(self):
        (self.root / "README.md").write_text("Public fixture", encoding="utf-8")
        with patch.object(scanner, "ROOT", self.root):
            files, findings = scanner.check()
        self.assertEqual(files, [self.root / "README.md"])
        self.assertEqual(findings, [])

    def test_vendor_bytes_survive_git_index_with_autocrlf(self):
        self.init_git()
        shutil.copy2(PROJECT / ".gitattributes", self.root / ".gitattributes")
        relative = Path("skills/imagegen-relay/third_party/relay-imagegen/scripts/image_client.py")
        target = self.root / relative
        target.parent.mkdir(parents=True)
        shutil.copy2(PROJECT / relative, target)
        self.git("-c", "core.autocrlf=true", "add", ".gitattributes", relative.as_posix())
        indexed = self.git("show", ":" + relative.as_posix()).stdout
        self.assertEqual(indexed, (PROJECT / relative).read_bytes())
        self.assertEqual(hashlib.sha256(indexed).hexdigest(), hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
