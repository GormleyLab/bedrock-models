"""Regression checks for shared agent configuration, using only the stdlib."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import sync_agent_config as sync
except ImportError:  # run from inside scripts/
    import sync_agent_config as sync


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

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_write_keeps_git_ignored_files_in_mirror(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".claude/skills/personal/\n")
        self.make_skill()
        personal = self.root / ".claude/skills/personal/SKILL.md"
        personal.parent.mkdir(parents=True)
        personal.write_text("Mine\n")
        (self.root / ".agents/skills/demo/SKILL.md").write_text("Updated\n")
        self.assertEqual(sync.write(self.root), [])
        self.assertEqual(personal.read_text(), "Mine\n")
        self.assertEqual((self.root / ".claude/skills/demo/SKILL.md").read_text(), "Updated\n")
        self.assertEqual(sync.check(self.root), [])

    def test_write_removes_deleted_skills_but_keeps_unmanaged_files(self):
        self.make_skill()
        cache = self.root / ".claude/skills/demo/__pycache__/x.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"cache")
        shutil.rmtree(self.root / ".agents/skills/demo")
        (self.root / ".agents/skills/other").mkdir()
        (self.root / ".agents/skills/other/SKILL.md").write_text("Other\n")
        self.assertEqual(sync.write(self.root), [])
        self.assertFalse((self.root / ".claude/skills/demo/SKILL.md").exists())
        self.assertTrue(cache.exists())
        self.assertTrue((self.root / ".claude/skills/other/SKILL.md").exists())

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_write_refuses_to_overwrite_unmanaged_mirror_file(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".claude/skills/demo/extra.md\n")
        self.make_skill()
        unmanaged = self.root / ".claude/skills/demo/extra.md"
        unmanaged.write_text("Local\n")
        (self.root / ".agents/skills/demo/extra.md").write_text("Canonical\n")
        self.assertTrue(any("unmanaged" in error for error in sync.write(self.root)))
        self.assertEqual(unmanaged.read_text(), "Local\n")

    def test_dot_claude_claude_md_is_allowed(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude/CLAUDE.md").write_text("Claude-only notes\n")
        self.assertEqual(sync.check(self.root), [])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_ignored_claude_md_is_not_checked(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("data/\n")
        (self.root / "data").mkdir()
        (self.root / "data/CLAUDE.md").write_text("Not ours\n")
        self.assertEqual(sync.check(self.root), [])

    def test_nested_claude_md_still_checked(self):
        (self.root / "sub").mkdir()
        (self.root / "sub/CLAUDE.md").write_text("Inline instructions\n")
        errors = sync.check(self.root)
        self.assertTrue(any("sub/CLAUDE.md: missing neighboring" in error for error in errors))
        self.assertTrue(any("sub/CLAUDE.md: must contain exactly" in error for error in errors))
        self.assertTrue(all("\\" not in error for error in errors))

        # All instruction diagnostics use the same portable relative path.
        (self.root / "sub/AGENTS.md").write_text("Scoped instructions\n")
        errors = sync.instruction_errors(self.root)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("sub/CLAUDE.md: must contain exactly"))

        (self.root / "sub/CLAUDE.md").write_bytes(b"\xff")
        self.assertEqual(
            sync.instruction_errors(self.root),
            ["sub/CLAUDE.md: must be UTF-8 text"],
        )

    def test_first_skill_creates_mirror_and_manifest(self):
        canonical = self.root / ".agents/skills/demo"
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("Demo\n")
        self.assertEqual(sync.write(self.root), [])
        self.assertEqual((self.root / ".claude/skills/demo/SKILL.md").read_text(), "Demo\n")
        self.assertTrue((self.root / ".agents/skills-sync-manifest.json").is_file())
        self.assertEqual(sync.check(self.root), [])

    def test_initial_sync_refuses_differing_existing_mirror(self):
        for location, text in ((".agents", "Demo\n"), (".claude", "Claude-only\n")):
            directory = self.root / location / "skills/demo"
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(text)
        self.assertTrue(sync.write(self.root))
        self.assertEqual((self.root / ".claude/skills/demo/SKILL.md").read_text(), "Claude-only\n")

    def test_claude_md_in_ignored_dirs_is_skipped(self):
        nested = self.root / "node_modules/pkg"
        nested.mkdir(parents=True)
        (nested / "CLAUDE.md").write_text("Not a wrapper\n")
        self.assertEqual(sync.check(self.root), [])

    def test_failed_copy_leaves_previous_mirror_and_allows_retry(self):
        canonical = self.make_skill("Version 1\n")
        (canonical / "SKILL.md").write_text("Version 2\n")
        original_copytree = sync.shutil.copytree

        def failing_copytree(*args, **kwargs):
            raise OSError("simulated copy failure")

        sync.shutil.copytree = failing_copytree
        try:
            with self.assertRaises(OSError):
                sync.write(self.root)
        finally:
            sync.shutil.copytree = original_copytree

        mirror = self.root / ".claude/skills/demo/SKILL.md"
        self.assertEqual(mirror.read_text(), "Version 1\n")
        self.assertFalse((self.root / ".claude/skills.sync-tmp").exists())
        self.assertEqual(sync.write(self.root), [])
        self.assertEqual(mirror.read_text(), "Version 2\n")
        self.assertFalse((self.root / ".claude/skills.sync-tmp").exists())
