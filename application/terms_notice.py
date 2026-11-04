"""
application/terms_notice.py

The informational notice shown before the bot starts working with a lock (owner decision): proceeding implies agreement with the project's
terms, with a link to the repository. It decides nothing and records nothing. The wearer's right to say no stays: "stop community review" in a DM.
"""

from __future__ import annotations

__all__ = ["REPOSITORY_URL", "TERMS_NOTICE"]

REPOSITORY_URL = "https://github.com/JohnTheSwitch/AI-Coach-Keyholder"
TERMS_NOTICE = (f"Before you lock: by proceeding you agree to how this project works, including community photo review on Chaster. "
                f"Details: {REPOSITORY_URL} . You can stop community review at any time by telling me.")
