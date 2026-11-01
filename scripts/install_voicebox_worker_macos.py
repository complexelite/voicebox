#!/usr/bin/env python3
"""Install the Voicebox GPU worker as a per-user macOS LaunchAgent."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
APP_DIR = HOME / "Library/LaunchAgents"
PLIST = APP_DIR / "com.complexelite.voicebox-worker.plist"
SOURCE = ROOT / "scripts/voicebox-worker-macos.sh"
AGENT_SOURCE = ROOT / "scripts/voicebox_remote_worker.py"
DEST = HOME / ".local/bin/voicebox-worker-macos.sh"
WORKER_BASE = HOME / "Library/Application Support/Voicebox Remote Worker"


def main() -> None:
    if os.uname().machine != "arm64":
        raise SystemExit("This installer is for Apple Silicon Macs.")
    if not Path("/Applications/Voicebox.app/Contents/MacOS/voicebox-server").is_file():
        raise SystemExit("Install Voicebox.app in /Applications first.")
    if not (HOME / ".config/voicebox/remote-worker.json").is_file():
        raise SystemExit("Enroll this Mac first with scripts/enroll_voicebox_worker.py.")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    WORKER_BASE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, DEST)
    shutil.copy2(AGENT_SOURCE, WORKER_BASE / "voicebox_remote_worker.py")
    DEST.chmod(0o700)
    (WORKER_BASE / "voicebox_remote_worker.py").chmod(0o600)
    log_path = WORKER_BASE / "launchd.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    definition = {
        "Label": "com.complexelite.voicebox-worker",
        # Keep a plugged-in Mac awake so it remains available as an inference
        # worker. macOS still sleeps the machine when it is running on battery.
        "ProgramArguments": ["/usr/bin/caffeinate", "-s", "/bin/bash", str(DEST)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "ProcessType": "Background",
    }
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as stream:
        plistlib.dump(definition, stream)
    PLIST.chmod(0o600)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], check=False)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
    print("Voicebox GPU worker installed and started. Its local API binds only to 127.0.0.1:17494.")
    print(f"Logs: {HOME / 'Library/Application Support/Voicebox Remote Worker/worker.log'}")


if __name__ == "__main__":
    main()
