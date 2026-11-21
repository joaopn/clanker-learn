"""Fixtures for the web server's unit tests.

The web server's modules (`server`, `services`) are found through web/'s path; the broker's
(`broker`) through outer/'s, so that the server is held to the broker's own rules. No module
of outer/ or sandbox/ is called `server` or `services`.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
for folder in ("outer", "web"):
    if str(REPO / folder) not in sys.path:
        sys.path.insert(0, str(REPO / folder))
