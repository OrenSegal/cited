"""Vercel entry point for cited hosted. The app lives in hosted/webapp.py;
the Vercel Python runtime serves the BaseHTTPRequestHandler named `handler`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hosted"))

from webapp import Handler as handler  # noqa: E402, F401
