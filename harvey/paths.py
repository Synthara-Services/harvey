"""Canonical filesystem locations for the Harvey checkout.

Harvey is repo-centric: config (harvey.yaml, .env), knowledge (prompts/,
skills/), and state (data/) all live next to the package. ``resolve()``
matters — when the package is reached through a symlink (some editable
installs), a bare ``Path(__file__).parent`` would point into
site-packages and Harvey would silently read/write the wrong files.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# What belongs to the user: the database, tokens and logs (data/), the skills
# folder (the shipped skills plus the ones training writes) and
# harvey.local.yaml. HARVEY_SANDBOX moves all three into another folder; the
# test suite points it at a temporary one so no test can touch the real files.
_SANDBOX = os.environ.get("HARVEY_SANDBOX", "").strip()
SANDBOXED = bool(_SANDBOX)
USER_ROOT = Path(_SANDBOX).resolve() if SANDBOXED else PROJECT_ROOT
DATA_DIR = USER_ROOT / "data"
SKILLS_DIR = USER_ROOT / "skills"
LOCAL_CONFIG = USER_ROOT / "harvey.local.yaml"
