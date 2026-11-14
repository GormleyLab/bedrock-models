"""Keep shared coding-agent instructions and skills in sync.

AGENTS.md and .agents/skills are canonical.  CLAUDE.md is only an import
wrapper, while .claude/skills is a generated mirror for Claude Code's skill
discovery.  The script deliberately refuses to overwrite unexpected edits in
either generated location.

The master copy lives in geppetto-ecosystem/scripts/.  Geppetto, Pinocchio and
geppetto-experiment-designer carry identical copies: change it there first and
copy it across, together with its tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

WRAPPER = "@AGENTS.md\n"
IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
# Operating-system clutter that is never part of a skill.  It is usually
# ignored by a global gitignore, so recording it in the manifest locally would
# make the committed manifest disagree with a clean CI checkout.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def ignored_path(path: Path, relative: Path) -> bool:
    return (
        any(part in IGNORED_DIRS for part in relative.parts)
        or path.name in IGNORED_NAMES
        or (path.is_file() and path.suffix in IGNORED_SUFFIXES)
    )


def git_ignored(directory: Path) -> set[Path]:
    """Absolute paths of files under ``directory`` that git ignores.

    Ignored files never reach CI, so they must not reach the mirror or the
    manifest either.  Outside a git work tree (or without git) this is empty.
    """
    if not directory.is_dir():
        return set()
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", "."],
            cwd=directory,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {
        (directory / name).resolve()
        for name in result.stdout.decode("utf-8", "surrogateescape").split("\0")
        if name
    }


def excluded(path: Path, relative: Path, ignored_by_git: set[Path]) -> bool:
    return ignored_path(path, relative) or path.resolve() in ignored_by_git


def copy_filter(ignored_by_git: set[Path]):
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if excluded(Path(directory) / name, Path(name), ignored_by_git)}

    return ignore


def normalized_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def snapshot(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    ignored_by_git = git_ignored(directory)
    result: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if not path.is_file():
            continue
        if excluded(path, relative, ignored_by_git):
            continue
        result[relative.as_posix()] = hashlib.sha256(normalized_bytes(path)).hexdigest()
    return result


def instruction_errors(root: Path) -> list[str]:
    errors: list[str] = []
    if not (root / "CLAUDE.md").is_file():
        errors.append("CLAUDE.md: missing required compatibility import '@AGENTS.md'")
    for path in sorted(root.rglob("CLAUDE.md")):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        agents_path = path.with_name("AGENTS.md")
        if not agents_path.is_file():
            errors.append(f"{relative}: missing neighboring AGENTS.md")
        try:
            contents = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            errors.append(f"{relative}: must be UTF-8 text")
            continue
        if contents != WRAPPER:
            errors.append(
                f"{relative}: must contain exactly '@AGENTS.md'; move all project "
                "instructions to the neighboring AGENTS.md"
            )
    return errors


def load_manifest(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise ValueError(f"invalid skill manifest: {path}")
    return data


def skill_errors(root: Path) -> list[str]:
    canonical = snapshot(root / ".agents" / "skills")
    mirror = snapshot(root / ".claude" / "skills")
    manifest_path = root / ".agents" / "skills-sync-manifest.json"
    manifest = load_manifest(manifest_path)
    configured = bool(canonical or mirror or manifest is not None)
    if not configured:
        return []

    errors: list[str] = []
    if canonical != mirror:
        errors.append(".claude/skills does not match canonical .agents/skills")
    if manifest is None:
        errors.append("missing .agents/skills-sync-manifest.json; run --write")
    elif canonical != manifest:
        errors.append("skill manifest is stale; run --write after reviewing mirror changes")
    return errors


def check(root: Path) -> list[str]:
    return instruction_errors(root) + skill_errors(root)


def write(root: Path) -> list[str]:
    errors = instruction_errors(root)
    if errors:
        return errors

    canonical_dir = root / ".agents" / "skills"
    mirror_dir = root / ".claude" / "skills"
    manifest_path = root / ".agents" / "skills-sync-manifest.json"
    canonical = snapshot(canonical_dir)
    mirror = snapshot(mirror_dir)
    manifest = load_manifest(manifest_path)

    if manifest is None:
        if mirror != canonical:
            return [
                "refusing initial sync because .claude/skills and .agents/skills differ; "
                "review the differences and make them identical first"
            ]
    elif mirror != manifest:
        return [
            "refusing to overwrite unexpected edits in .claude/skills; move intended "
            "changes to .agents/skills or restore the generated mirror first"
        ]

    if canonical_dir.is_dir():
        if mirror_dir.exists():
            shutil.rmtree(mirror_dir)
        shutil.copytree(
            canonical_dir,
            mirror_dir,
            ignore=copy_filter(git_ignored(canonical_dir)),
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(canonical, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        if mirror_dir.exists():
            shutil.rmtree(mirror_dir)
        if manifest_path.exists():
            manifest_path.unlink()
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift without changing files")
    mode.add_argument("--write", action="store_true", help="refresh generated skill mirrors")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        errors = check(root) if args.check else write(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]

    if errors:
        for error in errors:
            print(f"agent-config: {error}", file=sys.stderr)
        return 1
    print("agent-config: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
