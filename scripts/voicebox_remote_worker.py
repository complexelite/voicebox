#!/usr/bin/env python3
"""Outbound-only bridge between a local Voicebox inference server and a hub.

The local API stays bound to loopback. This process receives short-lived jobs
from the authenticated hub, mirrors only the selected voice profile, runs TTS
locally, then uploads the resulting WAV to that job's completion endpoint.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import platform
import tempfile
import time
from pathlib import Path

import httpx

LOG = logging.getLogger("voicebox.remote_worker")


class Worker:
    def __init__(self, hub: str, token: str, local_api: str, name: str, state_dir: Path):
        self.hub = hub.rstrip("/")
        self.token = token
        self.local_api = local_api.rstrip("/")
        self.name = name
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.map_file = state_dir / "profile-map.json"
        self.profile_map = self._load_map()
        self.hub_client = httpx.Client(timeout=httpx.Timeout(120, connect=10), headers={"X-Voicebox-Worker-Token": token})
        self.local_client = httpx.Client(timeout=httpx.Timeout(120, connect=5))
        local_key = os.environ.get("VOICEBOX_LOCAL_API_KEY", "").strip()
        if local_key:
            self.local_client.headers["Authorization"] = f"Bearer {local_key}"
        self.active_job_id: str | None = None
        self.last_heartbeat = 0.0
        self.last_capability_refresh = 0.0
        self.capabilities: dict = {"platform": f"{platform.system()} {platform.machine()}", "gpu": "unknown", "models": []}

    def _load_map(self) -> dict:
        try:
            return json.loads(self.map_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_map(self) -> None:
        temp = self.map_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.profile_map, indent=2))
        temp.chmod(0o600)
        temp.replace(self.map_file)

    def _local(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self.local_client.request(method, self.local_api + path, **kwargs)
        response.raise_for_status()
        return response

    def refresh_capabilities(self) -> None:
        health = self._local("GET", "/health").json()
        result = self._local("GET", "/models/status").json()
        models = []
        supported_engines = {
            "luxtts", "chatterbox", "chatterbox_turbo", "tada", "kokoro", "omnivoice",
            "qwen", "qwen_custom_voice", "qwen_voice_design",
        }
        for item in result.get("models", []):
            engine = item.get("engine")
            model_name = str(item.get("model_name", ""))
            if engine not in supported_engines:
                if model_name.startswith("qwen-tts-"):
                    engine = "qwen"
                elif model_name.startswith("qwen-custom-voice-"):
                    engine = "qwen_custom_voice"
                elif model_name.startswith("qwen-voice-design-"):
                    engine = "qwen_voice_design"
                elif model_name == "luxtts":
                    engine = "luxtts"
                elif model_name == "chatterbox-tts":
                    engine = "chatterbox"
                elif model_name == "chatterbox-turbo":
                    engine = "chatterbox_turbo"
                elif model_name.startswith("tada-"):
                    engine = "tada"
                elif model_name == "kokoro":
                    engine = "kokoro"
                elif model_name == "omnivoice":
                    engine = "omnivoice"
            if not item.get("downloaded") and not item.get("loaded"):
                continue
            if model_name.startswith(("qwen-tts-", "qwen-custom-voice-")):
                model_size = model_name.rsplit("-", 1)[-1]
                model_size = model_size.upper() if model_size[-1:].lower() == "b" else model_size
            else:
                model_size = item.get("model_size")
            if engine:
                models.append({"engine": engine, "model_size": model_size})
        gpu = health.get("gpu_type") or health.get("backend_type") or "CPU"
        self.capabilities = {
            "platform": f"{platform.system()} {platform.machine()}",
            "gpu": str(gpu)[:120],
            "models": models,
        }
        self.last_capability_refresh = time.monotonic()

    def heartbeat(self) -> None:
        self.hub_client.post("/worker/v1/heartbeat", json={
            "capabilities": self.capabilities,
            "active_job_id": self.active_job_id,
        }).raise_for_status()
        self.last_heartbeat = time.monotonic()

    def sync_profile(self, remote: dict) -> str:
        remote_id = remote["id"]
        mapped = self.profile_map.get(remote_id, {})
        local_id = mapped.get("local_id")
        profile_data = {
            "name": mapped.get("name") or f"Remote · {remote['name'][:60]} · {remote_id[:8]}",
            "description": remote.get("description"), "language": remote.get("language") or "en",
            "voice_type": remote.get("voice_type") or "cloned",
            "preset_engine": remote.get("preset_engine"), "preset_voice_id": remote.get("preset_voice_id"),
            "design_prompt": remote.get("design_prompt"), "default_engine": remote.get("default_engine"),
            "personality": remote.get("personality"),
        }
        if not local_id:
            created = self._local("POST", "/profiles", json=profile_data).json()
            local_id = created["id"]
            mapped = {"local_id": local_id, "name": profile_data["name"], "updated_at": ""}
        if mapped.get("updated_at") != remote.get("updated_at"):
            self._local("PUT", f"/profiles/{local_id}", json=profile_data)
            old_samples = self._local("GET", f"/profiles/{local_id}/samples").json()
            for sample in old_samples:
                self._local("DELETE", f"/profiles/samples/{sample['id']}")
            for sample in remote.get("samples", []):
                audio = base64.b64decode(sample["audio_base64"], validate=True)
                with tempfile.NamedTemporaryFile(
                    suffix=Path(sample.get("filename", "sample.wav")).suffix,
                    delete=False,
                ) as tmp:
                    tmp.write(audio)
                    temp_sample_path = Path(tmp.name)
                try:
                    with open(tmp.name, "rb") as audio_file:
                        self._local("POST", f"/profiles/{local_id}/samples", data={"reference_text": sample.get("reference_text") or " "}, files={"file": (sample.get("filename") or "sample.wav", audio_file, "audio/wav")})
                finally:
                    temp_sample_path.unlink(missing_ok=True)
            mapped["updated_at"] = remote.get("updated_at", "")
            self.profile_map[remote_id] = mapped
            self._save_map()
        return local_id

    def process_job(self, wrapped: dict) -> None:
        job_id = wrapped["id"]
        job = wrapped
        remote_profile = job.pop("profile")
        generation_id = job["generation_id"]
        self.active_job_id = job_id
        local_generation_id = None
        try:
            self.heartbeat()
            self.hub_client.post(f"/worker/v1/jobs/{job_id}/status", json={"status": "loading_model"}).raise_for_status()
            local_profile_id = self.sync_profile(remote_profile)
            generation_request = {
                "profile_id": local_profile_id, "text": job["text"],
                "language": job.get("language") or "en", "engine": job.get("engine") or "qwen",
                "model_size": job.get("model_size") or "1.7B", "seed": job.get("seed"),
                "instruct": job.get("instruct"), "normalize": False, "personality": False,
                "max_chunk_chars": job.get("max_chunk_chars") or 800,
                "crossfade_ms": job.get("crossfade_ms") if job.get("crossfade_ms") is not None else 50,
                "effects_chain": [],
            }
            local_generation_id = self._local("POST", "/generate", json=generation_request).json()["id"]
            reported_status = "loading_model"
            while True:
                time.sleep(3)
                if time.monotonic() - self.last_heartbeat >= 15:
                    self.heartbeat()
                status = self._local("GET", f"/history/{local_generation_id}").json()
                if status.get("status") in {"loading_model", "generating"} and status["status"] != reported_status:
                    reported_status = status["status"]
                    self.hub_client.post(
                        f"/worker/v1/jobs/{job_id}/status", json={"status": reported_status}
                    ).raise_for_status()
                if status.get("status") == "completed":
                    audio = self._local("GET", f"/audio/{local_generation_id}").content
                    result = self.hub_client.put(
                        f"/worker/v1/jobs/{job_id}/complete", content=audio,
                        headers={"Content-Type": "audio/wav"}, timeout=httpx.Timeout(240, connect=10),
                    )
                    result.raise_for_status()
                    LOG.info("Completed central generation %s", generation_id)
                    break
                if status.get("status") == "failed":
                    raise RuntimeError(status.get("error") or "Local Voicebox generation failed")
        except Exception as exc:
            LOG.exception("Generation %s failed on this worker", generation_id)
            try:
                self.hub_client.post(f"/worker/v1/jobs/{job_id}/fail", json={"status": "failed", "detail": str(exc)[:500]}).raise_for_status()
            except Exception:
                LOG.exception("Could not report job failure to Voicebox hub")
        finally:
            self.active_job_id = None

    def run(self) -> None:
        LOG.info("Connecting worker %s to %s", self.name, self.hub)
        backoff = 2
        while True:
            try:
                self.refresh_capabilities()
                self.heartbeat()
                backoff = 2
                while True:
                    if time.monotonic() - self.last_heartbeat >= 15:
                        self.heartbeat()
                    if time.monotonic() - self.last_capability_refresh >= 300:
                        self.refresh_capabilities()
                    response = self.hub_client.post("/worker/v1/jobs/claim", timeout=30)
                    response.raise_for_status()
                    job = response.json().get("job")
                    if job:
                        self.process_job(job)
                    else:
                        time.sleep(3)
            except KeyboardInterrupt:
                LOG.info("Worker stopped")
                return
            except Exception:
                LOG.exception("Worker connection or local Voicebox unavailable; retrying")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", default=os.environ.get("VOICEBOX_HUB_URL", "https://voicebox.complexelite.com"))
    parser.add_argument("--token", default=os.environ.get("VOICEBOX_WORKER_TOKEN"))
    parser.add_argument("--credential-file", default=str(Path.home() / ".config/voicebox/remote-worker.json"))
    parser.add_argument("--local-api", default=os.environ.get("VOICEBOX_LOCAL_API", "http://127.0.0.1:17493"))
    parser.add_argument("--name", default=os.environ.get("VOICEBOX_WORKER_NAME", platform.node()))
    parser.add_argument("--state-dir", default=os.environ.get("VOICEBOX_WORKER_STATE", str(Path.home() / ".voicebox-worker")))
    args = parser.parse_args()
    if not args.token:
        try:
            credential = json.loads(Path(args.credential_file).expanduser().read_text())
            args.token = credential["token"]
            args.hub = credential.get("hub_url", args.hub)
            args.name = credential.get("name", args.name)
        except (OSError, ValueError, KeyError):
            parser.error("Enroll this device first; worker credential file is missing or invalid.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Worker(args.hub, args.token, args.local_api, args.name, Path(args.state_dir)).run()


if __name__ == "__main__":
    main()
