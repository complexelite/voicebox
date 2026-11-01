#!/usr/bin/env python3
"""Enroll a device and save its scoped worker token without printing it."""

import argparse
import getpass
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", default="https://voicebox.complexelite.com")
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", default=str(Path.home() / ".config/voicebox/remote-worker.json"))
    args = parser.parse_args()
    key = getpass.getpass("Voicebox admin API key (not saved): ").strip()
    if len(key) < 32:
        raise SystemExit("The API key must be at least 32 characters.")
    request = urllib.request.Request(
        args.hub.rstrip("/") + "/workers/enroll",
        data=json.dumps({"name": args.name}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Enrollment failed: HTTP {exc.code}") from exc
    destination = Path(args.config).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential_data = json.dumps({
        "hub_url": args.hub.rstrip("/"), "name": args.name, "worker_id": data["id"],
        "token": data["token"],
    }, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".worker-", dir=destination.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(credential_data)
        os.replace(temp_name, destination)
        os.chmod(destination, 0o600)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    print(f"Enrolled {data['name']} ({data['id']}). Scoped credential saved to {destination} (mode 600).")


if __name__ == "__main__":
    main()
