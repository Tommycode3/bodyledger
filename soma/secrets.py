"""Credentials in the macOS Keychain, never in plaintext on disk.

The old sync's `coros.config.json` file kept the password in plaintext
next to the code. Here the secret never touches the filesystem: `security(1)`
writes it to the Keychain and reads it back only when needed.
"""
from __future__ import annotations

import json
import subprocess
from typing import Optional

ACCOUNT = "soma"


def set_secret(name: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-a", ACCOUNT, "-s", f"soma.{name}",
         "-w", value, "-U"],
        check=True, capture_output=True)


def get_secret(name: str) -> Optional[str]:
    p = subprocess.run(
        ["security", "find-generic-password", "-a", ACCOUNT, "-s", f"soma.{name}", "-w"],
        capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def delete_secret(name: str) -> bool:
    p = subprocess.run(
        ["security", "delete-generic-password", "-a", ACCOUNT, "-s", f"soma.{name}"],
        capture_output=True)
    return p.returncode == 0


def set_json(name: str, obj: dict) -> None:
    set_secret(name, json.dumps(obj))


def get_json(name: str) -> Optional[dict]:
    raw = get_secret(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
