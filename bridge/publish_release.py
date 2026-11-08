#!/usr/bin/env python3
"""Bake the live bridge URL/token into BOOTSTRAP.ps1 so it works from GitHub raw.

The sandbox preview URL (8788-<sandboxid>.e2b.app) dies with the sandbox - that
is what produced the 502. BOOTSTRAP.ps1 is static, so it can be served from
raw.githubusercontent.com permanently; it then probes for whichever bridge is
alive and hands over to that bridge's setup.ps1.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_DIR = os.path.join(HERE, "relay")
BOOTSTRAP = os.path.join(RELAY_DIR, "BOOTSTRAP.ps1")

BRIDGE = os.environ.get("BRIDGE_PUBLIC_BASE",
                        "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app")
TOKEN = os.environ.get("BRIDGE_TOKEN", "bridge-secret-iy8w5dbt2nw4x9qqg772j")


def bake(bridge: str, token: str) -> None:
    with open(BOOTSTRAP, encoding="utf-8") as fh:
        src = fh.read()
    block = ('$defaults = @(\n'
             f"    @{{ url = '{bridge}'; token = '{token}' }}\n"
             ')')
    src = re.sub(r"\$defaults = @\(.*?\n\)", block, src, count=1, flags=re.S)
    with open(BOOTSTRAP, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"baked bridge={bridge}")


def git(*args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{out.stderr}")
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default=BRIDGE)
    ap.add_argument("--token", default=TOKEN)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    bake(args.bridge, args.token)

    remote = git("remote", "get-url", "origin", check=False)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", check=False) or "main"
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote or "")
    owner, repo = (m.group(1), m.group(2)) if m else ("OWNER", "REPO")
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/bridge/relay/BOOTSTRAP.ps1"

    print("\nstable bootstrap URL:\n  " + raw)
    print('\none-liner:\n  iwr -UseBasicParsing "%s" -OutFile "$env:TEMP\\arena.ps1"; & "$env:TEMP\\arena.ps1"' % raw)

    if args.push:
        paths = ["bridge/relay/BOOTSTRAP.ps1", "bridge/relay/roblox_relay.ps1",
                 "bridge/relay/setup.ps1", "bridge/bridge.py", "bridge/roblox.py",
                 "bridge/SETUP.md", "bridge/README.md", ".gitignore", "bridge/publish_release.py"]
        git("add", *paths)
        if git("diff", "--cached", "--name-only"):
            git("commit", "-m", "Publish stable relay bootstrap (GitHub raw entry point)")
            git("push", "origin", branch)
            print(f"\npushed to {branch}")
        else:
            print("\nnothing to push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
