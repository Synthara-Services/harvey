"""The suite runs in a sandbox, and notices if anything touches the real files.

tests/conftest.py points data/, skills/ and harvey.local.yaml at a temporary
folder before harvey is imported, and fails the run if a real file there
changed. These check both halves.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harvey.config import BrainConfig
from tests.sandbox_guard import GENERATED_SKILLS, REPO, changed_files, fingerprint

SANDBOX = Path(os.environ["HARVEY_SANDBOX"]).resolve()


def inside_sandbox(path) -> bool:
    return Path(path).resolve().is_relative_to(SANDBOX)


# ── the redirect ──


def test_every_user_file_path_points_into_the_sandbox():
    from harvey import brain, dashboard, paths, state, trainer
    from harvey.integrations import gmail, linkedin

    for path in (
        paths.DATA_DIR, paths.SKILLS_DIR, paths.LOCAL_CONFIG,
        state.DB_PATH, brain.SKILLS_DIR,
        dashboard.DB_PATH, dashboard.PID_FILE, dashboard.LOG_FILE,
        gmail.TOKEN_FILE, linkedin.DATA_DIR, linkedin.COOKIES_PATH,
        trainer.Trainer(brain_config=BrainConfig()).skills_dir,
    ):
        assert inside_sandbox(path), path


def test_config_lookup_never_finds_the_real_local_config():
    from harvey import config, paths
    assert Path(config._find_config_file()).resolve() != (REPO / "harvey.local.yaml").resolve()

    paths.LOCAL_CONFIG.write_text("persona: {}\n", encoding="utf-8")
    try:
        assert Path(config._find_config_file()).resolve() == paths.LOCAL_CONFIG.resolve()
    finally:
        paths.LOCAL_CONFIG.unlink()


def test_the_sandbox_holds_the_shipped_skills_only():
    shipped = {p.name for p in (REPO / "skills").glob("*.md")} - GENERATED_SKILLS
    assert {p.name for p in (SANDBOX / "skills").glob("*.md")} >= shipped
    for name in GENERATED_SKILLS:
        assert not (SANDBOX / "skills" / name).exists()


def test_without_a_sandbox_paths_are_the_checkout():
    env = {k: v for k, v in os.environ.items() if k != "HARVEY_SANDBOX"}
    out = subprocess.run(
        [sys.executable, "-c",
         "import harvey.paths as p; print(p.DATA_DIR); print(p.SKILLS_DIR); print(p.LOCAL_CONFIG)"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    root = Path(__import__("harvey.paths").paths.PROJECT_ROOT)
    assert [Path(line).resolve() for line in out[:3]] == [
        (root / "data").resolve(), (root / "skills").resolve(), (root / "harvey.local.yaml").resolve(),
    ]


# ── the check ──


def make_tree(root: Path):
    (root / "skills").mkdir()
    (root / "data").mkdir()
    (root / "skills" / "product_knowledge.md").write_text("hand-written", encoding="utf-8")
    (root / "data" / "harvey.db").write_bytes(b"db")
    (root / "data" / "gmail_token.json").write_text("secret", encoding="utf-8")
    (root / "harvey.local.yaml").write_text("persona: {}\n", encoding="utf-8")


def test_the_check_reports_changed_added_and_removed_files(tmp_path):
    make_tree(tmp_path)
    before = fingerprint(tmp_path)
    assert changed_files(before, fingerprint(tmp_path)) == []

    (tmp_path / "skills" / "product_knowledge.md").write_text("test data", encoding="utf-8")
    (tmp_path / "skills" / "competitive_intel.md").write_text("new", encoding="utf-8")
    (tmp_path / "harvey.local.yaml").unlink()
    assert changed_files(before, fingerprint(tmp_path)) == [
        "added: skills/competitive_intel.md",
        "changed: skills/product_knowledge.md",
        "removed: harvey.local.yaml",
    ]


def test_credential_files_are_compared_without_being_read(tmp_path, monkeypatch):
    make_tree(tmp_path)
    token = tmp_path / "data" / "gmail_token.json"
    real_read = Path.read_bytes

    def guarded_read(self):
        assert "token" not in self.name, f"read {self}"
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    before = fingerprint(tmp_path)
    assert before["data/gmail_token.json"][0] == "meta"

    token.write_text("rotated", encoding="utf-8")
    os.utime(token, ns=(token.stat().st_atime_ns, token.stat().st_mtime_ns + 1_000_000))
    assert changed_files(before, fingerprint(tmp_path)) == ["changed: data/gmail_token.json"]


def run_suite_in_a_copy(tmp_path, test_body: str) -> subprocess.CompletedProcess:
    """Run pytest with the real conftest in a throwaway project whose own
    skills/ plays the part of the user's files."""
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "skills").mkdir()
    (project / "skills" / "product_knowledge.md").write_text("hand-written", encoding="utf-8")
    for name in ("__init__.py", "conftest.py", "sandbox_guard.py"):
        shutil.copy2(REPO / "tests" / name, project / "tests" / name)
    (project / "tests" / "test_it.py").write_text(test_body, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "HARVEY_SANDBOX"}
    # The run's own sandbox and backup land here, so pytest cleans them up.
    temp = tmp_path / "temp"
    temp.mkdir()
    env.update(TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=project, env=env, capture_output=True, text=True,
    )


def test_a_run_that_writes_a_real_file_fails(tmp_path):
    result = run_suite_in_a_copy(tmp_path, (
        "from pathlib import Path\n"
        "def test_writes_the_users_file():\n"
        "    root = Path(__file__).resolve().parent.parent\n"
        "    (root / 'skills' / 'product_knowledge.md').write_text('test data')\n"
    ))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "THE TEST RUN CHANGED REAL FILES" in result.stdout
    assert "changed: skills/product_knowledge.md" in result.stdout


def test_a_run_that_stays_in_the_sandbox_passes(tmp_path):
    result = run_suite_in_a_copy(tmp_path, (
        "import os\n"
        "from pathlib import Path\n"
        "def test_writes_only_the_sandbox():\n"
        "    sandbox = Path(os.environ['HARVEY_SANDBOX'])\n"
        "    (sandbox / 'skills' / 'product_knowledge.md').write_text('test data')\n"
    ))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHANGED REAL FILES" not in result.stdout
