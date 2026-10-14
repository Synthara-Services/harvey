"""Fingerprint the user's real files, to notice when a test run changes them.

No side effects on import: tests/conftest.py and the tests both use it.
"""

import hashlib
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Written by `harvey train` for one install; a test must never see them.
GENERATED_SKILLS = {"product_knowledge.md", "competitive_intel.md"}
# The real files no test may change.
WATCHED = ["skills", "data", "harvey.local.yaml", "harvey.yaml", ".env"]
CREDENTIAL_MARKERS = (".env", "token", "cookie", "secret")


def _is_credential(path: Path) -> bool:
    name = path.name.lower()
    return any(marker in name for marker in CREDENTIAL_MARKERS)


def fingerprint(root: Path, watched=WATCHED) -> dict[str, tuple]:
    """Every watched file under root, with a fingerprint of its state."""
    found = {}
    for entry in watched:
        base = root / entry
        files = sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else [base]
        for path in files:
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            stat = path.stat()
            if _is_credential(path):
                found[rel] = ("meta", stat.st_size, stat.st_mtime_ns)
            else:
                found[rel] = ("sha256", hashlib.sha256(path.read_bytes()).hexdigest())
    return found


def changed_files(before: dict, after: dict) -> list[str]:
    """Files added, removed or changed between two fingerprints."""
    return sorted(
        f"{'added' if rel not in before else 'removed' if rel not in after else 'changed'}: {rel}"
        for rel in set(before) | set(after)
        if before.get(rel) != after.get(rel)
    )


def backup_files(root: Path, files) -> Path:
    """Copy the non-credential watched files, to restore from if a run changes them."""
    backup = Path(tempfile.mkdtemp(prefix="harvey-test-backup-"))
    for rel, (kind, *_rest) in files.items():
        if kind == "sha256":
            target = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, target)
    return backup


def seed_sandbox() -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="harvey-test-sandbox-"))
    (sandbox / "data").mkdir()
    (sandbox / "skills").mkdir()
    for skill in (REPO / "skills").glob("*.md"):
        if skill.name not in GENERATED_SKILLS:
            shutil.copy2(skill, sandbox / "skills" / skill.name)
    return sandbox
