"""Regression checks for shared agent configuration, using only the stdlib."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import sync_agent_config as sync


class SyncAgentConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "AGENTS.md").write_text("Project instructions\n")
        (self.root / "CLAUDE.md").write_text(sync.WRAPPER)

    def make_skill(self, text: str = "Demo\n") -> Path:
        for location in (".agents", ".claude"):
            directory = self.root / location / "skills/demo"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "SKILL.md").write_text(text)
        self.assertEqual(sync.write(self.root), [])
        return self.root / ".agents/skills/demo"

    def test_missing_wrapper_is_rejected_by_check_and_write(self):
        (self.root / "CLAUDE.md").unlink()
        for operation in (sync.check, sync.write):
            with self.subTest(operation=operation.__name__):
                self.assertTrue(any("missing required" in error for error in operation(self.root)))

    def test_sync_excludes_local_artifacts_and_preserves_skill_assets(self):
        canonical = self.root / ".agents/skills/demo"
        mirror = self.root / ".claude/skills/demo"
        for directory in (canonical, mirror):
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text("Demo\n")
        self.assertEqual(sync.write(self.root), [])
        for name in (".venv", "node_modules", ".git", "build", "__pycache__"):
            artifact = canonical / "scripts" / name / "local-only"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("Local artifact\n")
        (canonical / "scripts/cache.pyc").write_bytes(b"cache")
        (canonical / "scripts/tool.py").write_text("print('hello')\n")
        (canonical / "SKILL.md").write_text("Updated demo\n")
        self.assertEqual(sync.write(self.root), [])
        self.assertEqual((mirror / "SKILL.md").read_text(), "Updated demo\n")
        self.assertEqual((mirror / "scripts/tool.py").read_text(), "print('hello')\n")
        self.assertEqual(sorted(path.name for path in (mirror / "scripts").iterdir()), ["tool.py"])
        self.assertEqual(sync.check(self.root), [])

    def test_sync_still_refuses_unexpected_mirror_edits(self):
        for location in (".agents", ".claude"):
            directory = self.root / location / "skills/demo"
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text("Demo\n")
        self.assertEqual(sync.write(self.root), [])
        mirror_file = self.root / ".claude/skills/demo/SKILL.md"
        mirror_file.write_text("Local edit\n")
        self.assertTrue(sync.write(self.root))
        self.assertEqual(mirror_file.read_text(), "Local edit\n")

    def test_os_clutter_stays_out_of_mirror_and_manifest(self):
        canonical = self.make_skill()
        (canonical / ".DS_Store").write_bytes(b"finder")
        (canonical / "Thumbs.db").write_bytes(b"explorer")
        self.assertEqual(sync.write(self.root), [])
        manifest = json.loads((self.root / ".agents/skills-sync-manifest.json").read_text())
        self.assertEqual(sorted(manifest), ["demo/SKILL.md"])
        self.assertFalse((self.root / ".claude/skills/demo/.DS_Store").exists())
        self.assertEqual(sync.check(self.root), [])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_ignored_files_stay_out_of_mirror_and_manifest(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("*.local\n")
        canonical = self.make_skill()
        (canonical / "secrets.local").write_text("token\n")
        self.assertEqual(sync.write(self.root), [])
        manifest = json.loads((self.root / ".agents/skills-sync-manifest.json").read_text())
        self.assertEqual(sorted(manifest), ["demo/SKILL.md"])
        self.assertFalse((self.root / ".claude/skills/demo/secrets.local").exists())
        self.assertEqual(sync.check(self.root), [])

    def test_check_reports_unsynced_canonical_edit(self):
        canonical = self.make_skill()
        (canonical / "SKILL.md").write_text("Edited without --write\n")
        errors = sync.check(self.root)
        self.assertTrue(any("does not match" in error for error in errors))
        self.assertTrue(any("manifest is stale" in error for error in errors))

    def test_check_reports_hand_edited_mirror(self):
        self.make_skill()
        (self.root / ".claude/skills/demo/SKILL.md").write_text("Hand edit\n")
        self.assertTrue(any("does not match" in error for error in sync.check(self.root)))

    def test_check_rejects_non_wrapper_claude_md(self):
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\nExtra instructions\n")
        self.assertTrue(any("must contain exactly" in error for error in sync.check(self.root)))
