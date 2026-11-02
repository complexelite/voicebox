# Remote GPU workers

Voicebox can hand eligible TTS generations to trusted home machines without exposing a home machine to inbound Internet traffic. Workers poll `https://voicebox.complexelite.com` over HTTPS with an individually revocable credential. The hub remains the source of truth for task status, audio files, and history. The VPS CPU queue remains the automatic fallback when no compatible worker is online; a lost worker lease is retried and then returned to the VPS queue.

## Browser sign-in

The hosted Voicebox web app uses the existing Perfex staff session and a PKCE-bound, one-time SSO hand-off. Start at `https://voicebox.complexelite.com`; unauthenticated browser visits redirect through Perfex and return to Voicebox. Voicebox exchanges the one-time code server-to-server and issues an eight-hour, HTTP-only, secure browser session cookie. The Voicebox API key stays server-side for service integrations such as Studio and is not entered into the browser. No additional shared SSO secret needs to be configured.

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

The LaunchAgent starts an isolated headless Voicebox API on loopback port 17494, uses the existing Hugging Face model cache, and starts the polling agent at login. It writes mirrored voice profiles and generation artifacts under `~/Library/Application Support/Voicebox Remote Worker/data`; those do not mix into the interactive Voicebox history. Logs are under `~/Library/Application Support/Voicebox Remote Worker/`. The worker sends macOS notifications when a job is assigned, starts generating, completes, or fails; notifications omit the voice script. macOS may require allowing notifications for `osascript`/Script Editor in System Settings. Set `VOICEBOX_DESKTOP_NOTIFICATIONS=0` in the LaunchAgent environment to turn them off.

## Windows NVIDIA worker

Install Voicebox for Windows on the Threadripper PC, open it once, and download the TTS model(s) you want that PC to serve. Confirm its local API is healthy at `http://127.0.0.1:17493/health`. Then run the installer from PowerShell as the Windows user who will keep the machine signed in (no Administrator shell is required):

```powershell
$installer = Join-Path $env:TEMP 'install-voicebox-worker.ps1'
Invoke-WebRequest 'https://raw.githubusercontent.com/complexelite/voicebox/main/scripts/install_voicebox_worker_windows.ps1' -OutFile $installer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
```

The installer checks the local Voicebox API/GPU, installs per-user Python 3.12 if needed, asks once for the Voicebox admin API key without saving it, and enrolls a dedicated worker credential. It stores the scoped credential under `%USERPROFILE%\.config\voicebox\remote-worker.json` with an ACL limited to that Windows user and SYSTEM. It registers a Task Scheduler job to reconnect at sign-in and restart if the process exits. Keep the desktop Voicebox backend available after sign-in; the worker retries if it takes time to start. The API must stay bound to `127.0.0.1`, never `0.0.0.0`.

Check the worker in Voicebox's authenticated `GET /workers` endpoint. Its model list contains only downloaded/loaded local models. The RTX 2060 Super should report CUDA when the Voicebox Windows CUDA backend is installed; if it reports CPU, install/enable CUDA in Voicebox before expecting GPU acceleration. Windows worker notifications use native Windows toast notifications while the worker runs in the signed-in user's session.

## Linux worker

Keep the Voicebox server backend running locally and bound to loopback (normally `http://127.0.0.1:17493`). Install Python 3.12+ and `httpx`, enroll the device, then run `scripts/voicebox_remote_worker.py` with the credential file. For persistence, configure a user `systemd` service to start after login/boot and restart on failure.

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
