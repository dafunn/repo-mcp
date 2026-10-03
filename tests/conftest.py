"""Sets GIT_BASE_URL and GIT_OWNERS, which `server` requires at import. The values
are placeholders and are never contacted.
"""
from __future__ import annotations

import os

os.environ.setdefault("GIT_BASE_URL", "https://git.example.com")
os.environ.setdefault("GIT_OWNERS", "example-org")
