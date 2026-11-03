"""Scoped remote inference workers for privately owned GPU machines.

Workers initiate all connections to Voicebox over HTTPS. Their one-time
enrollment token is exchanged for a per-worker token that can only heartbeat,
claim its compatible generation jobs, and report those jobs' results.
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, update
from sqlalchemy.orm import Session

from .. import config
from ..database import (
    Generation as DBGeneration,
    ProfileSample as DBProfileSample,
    RemoteVoiceJob,
    RemoteWorkerEnrollmentTicket,
    RemoteVoiceWorker,
    VoiceProfile as DBVoiceProfile,
    get_db,
)
from ..services import history
from ..utils.tasks import get_task_manager

router = APIRouter()
LEASE_SECONDS = 180
MAX_SAMPLE_BYTES_PER_JOB = 32 * 1024 * 1024
MAX_RESULT_BYTES = 120 * 1024 * 1024


class EnrollRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class EnrollmentTicketRequest(BaseModel):
    ticket: str = Field(min_length=40, max_length=100)


class HeartbeatRequest(BaseModel):
    capabilities: dict = Field(default_factory=dict)
    active_job_id: str | None = None


class JobStatusRequest(BaseModel):
    status: str
    detail: str | None = Field(default=None, max_length=500)


def _now() -> datetime:
    # SQLite returns naive values for these existing DateTime columns.
    return datetime.now(UTC).replace(tzinfo=None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _worker_from_token(token: str | None, db: Session) -> RemoteVoiceWorker:
    if not token or len(token) < 40:
        raise HTTPException(status_code=401, detail="Worker authentication required")
    worker = db.query(RemoteVoiceWorker).filter_by(token_hash=_hash_token(token)).first()
    if worker is None or not worker.enabled:
        raise HTTPException(status_code=401, detail="Worker authentication failed")
    return worker


def _compatible(worker: RemoteVoiceWorker, payload: dict) -> bool:
    caps = worker.capabilities or {}
    models = caps.get("models", [])
    engine = payload.get("engine") or "qwen"
    size = payload.get("model_size") or "1.7B"
    return any(
        isinstance(item, dict)
        and item.get("engine") == engine
        and (not item.get("model_size") or item.get("model_size") == size)
        for item in models
    )


def enqueue_remotely(db: Session, payload: dict) -> bool:
    """Persist a job for a live compatible home worker; caller falls back locally."""
    cutoff = _now() - timedelta(seconds=45)
    workers = (
        db.query(RemoteVoiceWorker)
        .filter(RemoteVoiceWorker.enabled.is_(True), RemoteVoiceWorker.last_seen_at >= cutoff)
        .all()
    )
    if not any(_compatible(worker, payload) for worker in workers):
        return False
    db.add(RemoteVoiceJob(
        id=str(uuid.uuid4()), generation_id=payload["generation_id"],
        payload=payload, status="queued", attempts=0,
    ))
    db.commit()
    return True


async def remote_job_reaper() -> None:
    """Recover abandoned leases and return long-waiting work to VPS inference."""
    import asyncio
    from ..database import session as db_session
    from ..services.generation import run_generation
    from ..services.task_queue import enqueue_generation

    while True:
        db = db_session.SessionLocal()
        fallback: list[dict] = []
        try:
            now = _now()
            rows = db.query(RemoteVoiceJob).filter(
                RemoteVoiceJob.status.in_(["queued", "running"])
            ).all()
            for job in rows:
                if job.status == "running" and job.lease_until and job.lease_until < now:
                    job.status = "queued"
                    job.worker_id = None
                    job.lease_until = None
                age = (now - (job.created_at or now)).total_seconds()
                if (job.status == "queued" and (job.attempts >= 2 or age >= 300)):
                    payload = dict(job.payload or {})
                    gen = db.query(DBGeneration).filter_by(id=job.generation_id).first()
                    if gen and gen.status in {"generating", "loading_model"}:
                        job.status = "cpu_fallback"
                        job.worker_id = None
                        job.lease_until = None
                        fallback.append(payload)
                    else:
                        job.status = "cancelled"
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

        for payload in fallback:
            try:
                get_task_manager().start_generation(
                    task_id=payload["generation_id"], profile_id=payload["profile_id"], text=payload["text"]
                )
                enqueue_generation(
                    payload["generation_id"],
                    run_generation(
                        generation_id=payload["generation_id"], profile_id=payload["profile_id"],
                        text=payload["text"], language=payload.get("language", "en"),
                        engine=payload.get("engine", "qwen"), model_size=payload.get("model_size", "1.7B"),
                        seed=payload.get("seed"), instruct=payload.get("instruct"),
                        normalize=payload.get("normalize", False), effects_chain=payload.get("effects_chain"),
                        mode=payload.get("mode", "generate"), max_chunk_chars=payload.get("max_chunk_chars"),
                        crossfade_ms=payload.get("crossfade_ms"), version_id=payload.get("version_id"),
                    ),
                )
            except Exception:
                # Queue initialization failures are surfaced through the existing
                # generation status recovery path rather than losing the row.
                pass
        await asyncio.sleep(15)


def _serialize_job(job: RemoteVoiceJob, db: Session) -> dict:
    payload = dict(job.payload or {})
    profile = db.query(DBVoiceProfile).filter_by(id=payload["profile_id"]).first()
    if profile is None:
        raise HTTPException(status_code=409, detail="The voice profile no longer exists")
    samples = db.query(DBProfileSample).filter_by(profile_id=profile.id).all()
    sample_payload = []
    total_bytes = 0
    for sample in samples:
        sample_path = config.resolve_storage_path(sample.audio_path)
        if sample_path is None or not sample_path.is_file():
            raise HTTPException(status_code=409, detail="A voice profile sample is unavailable")
        audio = sample_path.read_bytes()
        total_bytes += len(audio)
        if total_bytes > MAX_SAMPLE_BYTES_PER_JOB:
            raise HTTPException(status_code=413, detail="Voice profile samples exceed the worker transfer limit")
        sample_payload.append({
            "id": sample.id,
            "reference_text": sample.reference_text,
            "filename": f"{sample.id}{sample_path.suffix or '.wav'}",
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        })
    payload["profile"] = {
        "id": profile.id, "name": profile.name, "description": profile.description,
        "language": profile.language, "voice_type": profile.voice_type,
        "preset_engine": profile.preset_engine, "preset_voice_id": profile.preset_voice_id,
        "design_prompt": profile.design_prompt, "default_engine": profile.default_engine,
        "personality": profile.personality, "updated_at": profile.updated_at.isoformat() if profile.updated_at else "",
        "samples": sample_payload,
    }
    return payload


@router.post("/workers/enroll")
async def enroll_worker(data: EnrollRequest, db: Session = Depends(get_db)):
    """Create a worker credential. This endpoint requires the normal Voicebox admin API key."""
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Worker name is required")
    token = secrets.token_urlsafe(48)
    worker = RemoteVoiceWorker(
        id=str(uuid.uuid4()), name=name, token_hash=_hash_token(token),
        capabilities={"models": []}, enabled=True,
    )
    db.add(worker)
    db.commit()
    db.refresh(worker)
    return {"id": worker.id, "name": worker.name, "token": token, "tokenShownOnce": True}


@router.post("/workers/setup/windows")
async def download_windows_worker_setup(data: EnrollRequest, request: Request, db: Session = Depends(get_db)):
    """Issue a one-use enrollment ticket and return a double-click Windows installer bundle."""
    from ..services import perfex_sso

    user = perfex_sso.session_user(request)
    if user is None:
        raise HTTPException(status_code=403, detail="Sign in through Perfex before downloading a worker installer")
    name = " ".join(data.name.split())
    if not name:
        raise HTTPException(status_code=400, detail="Worker name is required")
    script_path = Path(__file__).resolve().parents[1] / "install_voicebox_worker_windows.ps1"
    if not script_path.is_file():
        raise HTTPException(status_code=503, detail="The Windows worker installer is not available on this Voicebox build")
    now = _now()
    db.query(RemoteWorkerEnrollmentTicket).filter(
        RemoteWorkerEnrollmentTicket.expires_at <= now
    ).delete(synchronize_session=False)
    ticket = secrets.token_urlsafe(36)
    db.add(RemoteWorkerEnrollmentTicket(
        id=str(uuid.uuid4()), token_hash=_hash_token(ticket), worker_name=name,
        requested_by=str(user["sub"]), created_at=now, expires_at=now + timedelta(minutes=30),
    ))
    db.commit()

    script = script_path.read_text(encoding="utf-8")
    # Both fields are inserted as PowerShell single-quoted literals; escape quotes
    # so user-provided device names remain inert inside the script.
    safe_ticket = ticket.replace("'", "''")
    safe_name = name.replace("'", "''")
    script = script.replace("'__VOICEBOX_ENROLLMENT_TICKET__'", f"'{safe_ticket}'")
    script = script.replace("'__VOICEBOX_WORKER_NAME__'", f"'{safe_name}'")
    if "__VOICEBOX_ENROLLMENT_TICKET__" in script or "__VOICEBOX_WORKER_NAME__" in script:
        raise HTTPException(status_code=503, detail="The Windows worker installer template is invalid")
    launcher = r'''@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Voicebox-Worker.ps1"
if errorlevel 1 (
  echo.
  echo Setup did not complete. Read the message above, fix the issue, then run this file again.
  pause
  exit /b 1
)
echo.
echo Voicebox worker setup finished. You can close this window.
pause
'''
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("Install-Voicebox-Worker.ps1", script)
        bundle.writestr("Install-Voicebox-Worker.cmd", launcher)
        bundle.writestr("README.txt", (
            "Voicebox Windows remote GPU worker\r\n\r\n"
            "1. Extract this ZIP on the Windows PC that has Voicebox installed.\r\n"
            "2. Open Voicebox and ensure its local backend is running.\r\n"
            "3. Double-click Install-Voicebox-Worker.cmd and accept the Windows script prompt if shown.\r\n"
            "4. Leave the Windows account signed in; the worker starts at sign-in.\r\n\r\n"
            "This setup ticket expires in 30 minutes and can enroll one worker once. Download a fresh installer if it expires.\r\n"
            "The installer never asks for your Voicebox admin key.\r\n"
        ))
    return Response(
        archive.getvalue(), media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="voicebox-windows-worker.zip"',
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/workers/enroll-ticket")
async def enroll_worker_with_ticket(
    data: EnrollmentTicketRequest, response: Response, db: Session = Depends(get_db)
):
    """Redeem a ticket from the authenticated Windows setup download exactly once."""
    now = _now()
    row = db.query(RemoteWorkerEnrollmentTicket).filter_by(token_hash=_hash_token(data.ticket)).first()
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        raise HTTPException(status_code=401, detail="The setup ticket expired or was already used. Download a fresh installer from Voicebox Settings > Workers.")

    consumed = db.execute(update(RemoteWorkerEnrollmentTicket).where(and_(
        RemoteWorkerEnrollmentTicket.id == row.id,
        RemoteWorkerEnrollmentTicket.consumed_at.is_(None),
        RemoteWorkerEnrollmentTicket.expires_at > now,
    )).values(consumed_at=now))
    if consumed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="The setup ticket expired or was already used. Download a fresh installer from Voicebox Settings > Workers.")

    token = secrets.token_urlsafe(48)
    worker = RemoteVoiceWorker(
        id=str(uuid.uuid4()), name=row.worker_name, token_hash=_hash_token(token),
        capabilities={"models": []}, enabled=True,
    )
    db.add(worker)
    db.commit()
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return {"id": worker.id, "name": worker.name, "token": token, "tokenShownOnce": True}


@router.get("/workers")
async def list_workers(db: Session = Depends(get_db)):
    cutoff = _now() - timedelta(seconds=45)
    rows = db.query(RemoteVoiceWorker).order_by(RemoteVoiceWorker.created_at.desc()).all()
    response = []
    for w in rows:
        active = db.query(RemoteVoiceJob, DBGeneration).join(
            DBGeneration, RemoteVoiceJob.generation_id == DBGeneration.id
        ).filter(
            RemoteVoiceJob.worker_id == w.id,
            RemoteVoiceJob.status == "running",
        ).order_by(RemoteVoiceJob.created_at.desc()).first()
        response.append({
        "id": w.id, "name": w.name, "enabled": w.enabled,
        "online": bool(w.last_seen_at and w.last_seen_at >= cutoff),
        "lastSeenAt": w.last_seen_at.isoformat() if w.last_seen_at else None,
        "capabilities": w.capabilities or {},
        "activeJob": ({
            "generationId": active[1].id,
            "textPreview": (active[1].text or "")[:120],
            "status": active[1].status,
            "startedAt": active[0].updated_at.isoformat() if active[0].updated_at else None,
        } if active else None),
    })
    return response


@router.get("/workers/jobs")
async def list_worker_jobs(db: Session = Depends(get_db)):
    """Return the recent remote queue with assignment and execution state."""
    rows = db.query(RemoteVoiceJob, DBGeneration, RemoteVoiceWorker.name).join(
        DBGeneration, RemoteVoiceJob.generation_id == DBGeneration.id
    ).outerjoin(
        RemoteVoiceWorker, RemoteVoiceJob.worker_id == RemoteVoiceWorker.id
    ).order_by(RemoteVoiceJob.created_at.desc()).limit(40).all()
    counts = {key: 0 for key in ("queued", "claimed", "generating", "completed", "failed", "cpu_fallback")}
    jobs = []
    for job, generation, worker_name in rows:
        state = job.status
        if state == "running":
            state = "generating" if generation.status == "generating" else "claimed"
        if state in counts:
            counts[state] += 1
        jobs.append({
            "id": job.id,
            "generationId": generation.id,
            "textPreview": (generation.text or "")[:120],
            "profileId": generation.profile_id,
            "engine": generation.engine or "qwen",
            "modelSize": generation.model_size,
            "generationStatus": generation.status,
            "status": state,
            "workerName": worker_name,
            "createdAt": job.created_at.isoformat() if job.created_at else None,
            "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
        })
    return {"counts": counts, "jobs": jobs}


@router.delete("/workers/{worker_id}")
async def revoke_worker(worker_id: str, db: Session = Depends(get_db)):
    worker = db.query(RemoteVoiceWorker).filter_by(id=worker_id).first()
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    worker.enabled = False
    db.query(RemoteVoiceJob).filter_by(worker_id=worker_id, status="running").update(
        {"status": "queued", "worker_id": None, "lease_until": None}, synchronize_session=False
    )
    db.commit()
    return {"ok": True, "revoked": worker_id}


@router.post("/worker/v1/heartbeat")
async def worker_heartbeat(
    data: HeartbeatRequest,
    x_voicebox_worker_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    worker = _worker_from_token(x_voicebox_worker_token, db)
    worker.last_seen_at = _now()
    # Bound data accepted from a worker; this is routing metadata, not a log sink.
    models = data.capabilities.get("models", [])
    if not isinstance(models, list) or len(models) > 200:
        raise HTTPException(status_code=400, detail="Invalid model capability list")
    worker.capabilities = {
        "platform": str(data.capabilities.get("platform", "unknown"))[:40],
        "gpu": str(data.capabilities.get("gpu", "unknown"))[:120],
        "models": [m for m in models if isinstance(m, dict) and len(str(m)) < 300][:200],
    }
    if data.active_job_id:
        job = db.query(RemoteVoiceJob).filter_by(
            id=data.active_job_id, worker_id=worker.id, status="running"
        ).first()
        if job:
            job.lease_until = _now() + timedelta(seconds=LEASE_SECONDS)
    db.commit()
    return {"ok": True, "serverTime": _now().isoformat()}


@router.post("/worker/v1/jobs/claim")
async def claim_job(
    x_voicebox_worker_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    worker = _worker_from_token(x_voicebox_worker_token, db)
    now = _now()
    expired = db.query(RemoteVoiceJob).filter(
        RemoteVoiceJob.status == "running", RemoteVoiceJob.lease_until < now
    ).all()
    for row in expired:
        row.status = "queued"
        row.worker_id = None
        row.lease_until = None
    worker.last_seen_at = now
    db.commit()

    for candidate in db.query(RemoteVoiceJob).filter_by(status="queued").order_by(RemoteVoiceJob.created_at.asc()).limit(50).all():
        # A conditional update makes claim single-winner even if two agents
        # poll simultaneously against SQLite.
        job = db.query(RemoteVoiceJob).filter_by(id=candidate.id, status="queued").first()
        if job is None:
            continue
        if not _compatible(worker, job.payload or {}):
            continue
        changed = db.query(RemoteVoiceJob).filter_by(id=job.id, status="queued").update({
            "status": "running", "worker_id": worker.id,
            "attempts": RemoteVoiceJob.attempts + 1,
            "lease_until": now + timedelta(seconds=LEASE_SECONDS),
        }, synchronize_session=False)
        if not changed:
            db.rollback()
            continue
        db.refresh(job)
        gen = db.query(DBGeneration).filter_by(id=job.generation_id).first()
        if gen and gen.status in ("generating", "loading_model"):
            gen.status = "loading_model"
            gen.generation_started_at = now
            gen.generation_time = None
            db.commit()
            return {"job": {"id": job.id, **_serialize_job(job, db)}}
        job.status = "cancelled"
        job.lease_until = None
        db.commit()
    return {"job": None}


@router.post("/worker/v1/jobs/{job_id}/status")
async def update_job_status(
    job_id: str,
    data: JobStatusRequest,
    x_voicebox_worker_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    worker = _worker_from_token(x_voicebox_worker_token, db)
    job = db.query(RemoteVoiceJob).filter_by(id=job_id, worker_id=worker.id, status="running").first()
    if job is None:
        raise HTTPException(status_code=404, detail="Active job not found")
    if data.status not in {"loading_model", "generating"}:
        raise HTTPException(status_code=400, detail="Unsupported worker job status")
    gen = db.query(DBGeneration).filter_by(id=job.generation_id).first()
    if gen and gen.status in {"loading_model", "generating"}:
        gen.status = data.status
        db.commit()
    return {"ok": True}


@router.put("/worker/v1/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    request: Request,
    x_voicebox_worker_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    worker = _worker_from_token(x_voicebox_worker_token, db)
    job = db.query(RemoteVoiceJob).filter_by(id=job_id, worker_id=worker.id, status="running").first()
    if job is None:
        raise HTTPException(status_code=404, detail="Active job not found")
    audio_buffer = bytearray()
    async for chunk in request.stream():
        audio_buffer.extend(chunk)
        if len(audio_buffer) > MAX_RESULT_BYTES:
            raise HTTPException(status_code=413, detail="Generated audio exceeds the upload limit")
    audio_bytes = bytes(audio_buffer)
    if not audio_bytes:
        raise HTTPException(status_code=413, detail="Generated audio is empty or exceeds the upload limit")
    import tempfile
    from ..utils.audio import load_audio, save_audio
    from ..services.generation import _save_generate, _save_regenerate, _save_retry

    gen = db.query(DBGeneration).filter_by(id=job.generation_id).first()
    if gen is None:
        raise HTTPException(status_code=404, detail="Generation record no longer exists")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        audio, sample_rate = load_audio(tmp_path)
        payload = job.payload or {}
        if payload.get("normalize") or payload.get("mode") == "regenerate":
            from ..utils.audio import normalize_audio
            audio = normalize_audio(audio)
        duration = float(len(audio) / sample_rate)
        if payload.get("mode") == "retry":
            audio_path = _save_retry(generation_id=gen.id, audio=audio, sample_rate=sample_rate, save_audio=save_audio)
        elif payload.get("mode") == "regenerate":
            audio_path = _save_regenerate(
                generation_id=gen.id, version_id=payload.get("version_id"), audio=audio,
                sample_rate=sample_rate, save_audio=save_audio, db=db,
            )
        else:
            audio_path = _save_generate(
                generation_id=gen.id, audio=audio, sample_rate=sample_rate,
                effects_chain=payload.get("effects_chain"), save_audio=save_audio, db=db,
            )
        await history.update_generation_status(
            generation_id=gen.id, status="completed", db=db,
            audio_path=audio_path, duration=duration,
            generation_time=(
                max(0.0, (_now() - gen.generation_started_at.replace(tzinfo=UTC)).total_seconds())
                if gen.generation_started_at else None
            ),
        )
        job.status = "completed"
        job.lease_until = None
        db.commit()
        get_task_manager().complete_generation(gen.id)
        return {"ok": True, "generation_id": gen.id, "duration": duration}
    except HTTPException:
        raise
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:1000]
        job.lease_until = None
        db.commit()
        await history.update_generation_status(gen.id, "failed", db, error="Remote worker result could not be stored")
        get_task_manager().complete_generation(gen.id)
        raise HTTPException(status_code=422, detail="Generated audio could not be validated or stored") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@router.post("/worker/v1/jobs/{job_id}/fail")
async def fail_job(
    job_id: str,
    data: JobStatusRequest,
    x_voicebox_worker_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    worker = _worker_from_token(x_voicebox_worker_token, db)
    job = db.query(RemoteVoiceJob).filter_by(id=job_id, worker_id=worker.id, status="running").first()
    if job is None:
        raise HTTPException(status_code=404, detail="Active job not found")
    # Retry on another GPU worker where possible. After two attempts the
    # reaper moves this job to the existing VPS queue instead of stranding it.
    job.status = "queued"
    job.error = (data.detail or "Worker generation failed")[:500]
    job.worker_id = None
    job.lease_until = None
    db.commit()
    return {"ok": True, "retrying": True, "fallbackAfterAttempts": 2}
