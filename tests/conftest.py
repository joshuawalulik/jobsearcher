"""Seed config/profile.yaml from the example when it doesn't exist.

The app (and a few integration tests) read the checked-in profile
directly; a fresh clone only ships config/profile.example.yaml, so the
suite runs against that. An existing profile.yaml is never touched.
"""

import shutil
from pathlib import Path

_CONFIG = Path(__file__).parent.parent / "config"
_PROFILE = _CONFIG / "profile.yaml"

if not _PROFILE.exists():
    shutil.copy(_CONFIG / "profile.example.yaml", _PROFILE)
