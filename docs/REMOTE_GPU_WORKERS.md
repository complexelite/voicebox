# Remote GPU workers

Voicebox can hand eligible TTS generations to trusted home machines without exposing a home machine to inbound Internet traffic. Workers poll `https://voicebox.complexelite.com` over HTTPS with an individually revocable credential. The hub remains the source of truth for task status, audio files, and history. The VPS CPU queue remains the automatic fallback when no compatible worker is online; a lost worker lease is retried and then returned to the VPS queue.

## Security and data flow

- A worker credential can only heartbeat, claim compatible jobs, report progress, upload a result, or report failure. It cannot access the general Voicebox API.
- The hub stores only a SHA-256 hash of each worker token. The enrollment API returns the token once; the enrollment helper stores it in a user-only credential file and does not print it.
- A job transfers its text, generation settings, selected voice-profile metadata, and reference samples to the home machine over TLS. The worker mirrors the profile into its local Voicebox database and retains that local copy so the model can use it. The generated WAV is uploaded to the hub and becomes the canonical history result.
- The local inference API binds to `127.0.0.1` only. No port-forwarding, public DNS, or inbound firewall opening is needed.
- Workers process one generation at a time each. Two compatible workers can process separate queued jobs in parallel.

## Enroll a worker

Run from a Voicebox source checkout on each worker machine:

```sh
python3 scripts/enroll_voicebox_worker.py --name "M2 Max"
```

The helper prompts for the Voicebox admin API key without echoing or saving it, then stores a per-worker token in `~/.config/voicebox/remote-worker.json` with mode `600`. Enroll each machine separately so they have different revocation controls. Never copy a worker token into a task, ticket, or chat.

## Apple Silicon Mac

Install `Voicebox.app` in `/Applications`, enroll the Mac, then run:

```sh
python3 scripts/install_voicebox_worker_macos.py
```

The LaunchAgent starts an isolated headless Voicebox API on loopback port 17494, uses the existing Hugging Face model cache, and starts the polling agent at login. It writes mirrored voice profiles and generation artifacts under `~/Library/Application Support/Voicebox Remote Worker/data`; those do not mix into the interactive Voicebox history. Logs are under `~/Library/Application Support/Voicebox Remote Worker/`.

## Windows or Linux worker

Keep the Voicebox desktop/server backend running locally and bound to loopback (normally `http://127.0.0.1:17493`). Install Python 3.10+ and `httpx`, enroll the device, then start the agent:

```sh
python -m pip install httpx
python scripts/voicebox_remote_worker.py --credential-file "$HOME/.config/voicebox/remote-worker.json" --local-api http://127.0.0.1:17493
```

On Windows, use the credential file under `%USERPROFILE%\.config\voicebox\remote-worker.json` and run the same Python script from PowerShell with that path. For a permanent worker, register the command in Task Scheduler (Windows) or a user `systemd` service (Linux), configured to start after login/boot and restart on failure. Keep the Voicebox server API local-only; do not bind it to `0.0.0.0`.

Install or download the model(s) you intend to use on each worker. First use may download missing model files into the local Voicebox model cache, so that initial generation can take longer. Engine availability differs by platform: Apple Silicon advertises engines available to its MLX backend; NVIDIA should use a CUDA-enabled local Voicebox installation. The agent advertises the local engine registry and the hub will only dispatch a matching engine.

## Manage workers

The authenticated API lists workers and their recent platform/GPU/model capabilities:

```text
GET https://voicebox.complexelite.com/workers
```

Revoke a lost or retired machine with:

```text
DELETE https://voicebox.complexelite.com/workers/{worker_id}
```

Both management endpoints require the normal Voicebox admin API key. Revocation invalidates the device token and returns its active job to the queue. Do not send admin keys to worker devices.

## Operational behavior

- Voicebox keeps the existing per-machine serial inference queue; each GPU worker handles one active generation at a time.
- Worker heartbeat leases expire after three minutes. Unfinished work can be reclaimed by another compatible device. After repeated worker failures or five minutes without a worker, Voicebox returns the job to the existing VPS CPU queue.
- Voicebox remains usable if either or both home machines are offline. Jobs submitted while there is no compatible worker go directly to the VPS queue.
- Generated audio and profile records remain on the VPS as the canonical result; the worker's local database is an execution cache.
