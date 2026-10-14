"""Keep every test run away from the user's own files.

1. Before anything imports harvey, HARVEY_SANDBOX points data/, skills/ and
   harvey.local.yaml at a temporary folder, seeded with the shipped skills
   (never the ones training generated for this install).
2. The Gmail and LinkedIn modules build their own data/ paths; they are
   pointed at the sandbox as well.
3. Backstop for anything that still reaches the real files, such as old code
   run in a mutation check: the real files are fingerprinted before the run
   and again after it, and the run fails if any changed. A copy taken before
   the run (credentials excluded) is kept for restoring. Credential files are
   compared by size and modification time only, never read or copied.
"""

import os
import shutil

import pytest

from tests.sandbox_guard import REPO, backup_files, seed_sandbox, changed_files, fingerprint

# Runs when pytest loads this file: before any test module imports harvey.
SANDBOX = seed_sandbox()
os.environ["HARVEY_SANDBOX"] = str(SANDBOX)
BEFORE = fingerprint(REPO)
BACKUP = backup_files(REPO, BEFORE)

import harvey.integrations.gmail as _gmail  # noqa: E402
import harvey.integrations.linkedin as _linkedin  # noqa: E402

_gmail.TOKEN_FILE = SANDBOX / "data" / "gmail_token.json"
_linkedin.DATA_DIR = SANDBOX / "data"
_linkedin.COOKIES_PATH = SANDBOX / "data" / "linkedin_cookies.json"


def pytest_sessionfinish(session, exitstatus):
    changed = changed_files(BEFORE, fingerprint(REPO))
    if changed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        lines = "\n".join(f"  {c}" for c in changed)
        print(
            "\n\nTHE TEST RUN CHANGED REAL FILES. Tests must only write inside "
            f"{SANDBOX}.\n{lines}\n"
            f"Copies from before the run (credentials excluded): {BACKUP}\n"
        )
        return
    shutil.rmtree(SANDBOX, ignore_errors=True)
    shutil.rmtree(BACKUP, ignore_errors=True)
