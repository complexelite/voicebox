"""Voice profile endpoints."""

import asyncio
import io
import json as _json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from .. import config, models
from ..utils.http import safe_content_disposition
from ..database import VoiceProfile as DBVoiceProfile, get_db
from ..services import channels, export_import, personality, profiles
from ..services.profiles import _profile_to_response

logger = logging.getLogger(__name__)

router = APIRouter()

MEDIA_EXTRACT_MAX_BYTES = 250 * 1024 * 1024
MEDIA_EXTRACT_CHUNK_SIZE = 1024 * 1024
MEDIA_EXTRACT_MAX_SECONDS = 29.0
MEDIA_EXTRACT_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg",
    ".opus", ".wav", ".webm", ".wma",
}


@router.post("/profiles/extract-sample")
async def extract_profile_sample(
    file: UploadFile = File(...),
    start_seconds: float = Form(...),
    end_seconds: float = Form(...),
):
    """Extract a short WAV clip from user-provided media; never fetch media from a URL."""
    from ..utils.ffmpeg import find_ffmpeg

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in MEDIA_EXTRACT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Choose a supported audio or video file.")
    if not math.isfinite(start_seconds) or not math.isfinite(end_seconds):
        raise HTTPException(status_code=400, detail="Clip start and end must be valid times.")
    clip_duration = end_seconds - start_seconds
    if start_seconds < 0 or clip_duration <= 0 or clip_duration > MEDIA_EXTRACT_MAX_SECONDS:
        raise HTTPException(status_code=400, detail="Choose a clip up to 29 seconds long.")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="Audio extraction requires FFmpeg on the Voicebox server.")

    source_path: Path | None = None
    output_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source:
            source_path = Path(source.name)
            total_size = 0
            while chunk := await file.read(MEDIA_EXTRACT_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MEDIA_EXTRACT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Source media exceeds the 250 MB limit.")
                source.write(chunk)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as output:
            output_path = Path(output.name)

        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start_seconds:.3f}", "-i", str(source_path),
            "-t", f"{clip_duration:.3f}", "-vn", "-ac", "1", "-ar", "24000",
            "-c:a", "pcm_s16le", str(output_path),
        ]

        def run_extractor() -> subprocess.CompletedProcess:
            return subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)

        result = await asyncio.to_thread(run_extractor)
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 44:
            detail = (result.stderr or "No audio was found in the selected range.").strip()[:500]
            raise HTTPException(status_code=422, detail=f"Could not extract audio from that range: {detail}")

        response_path = output_path
        output_path = None
        return FileResponse(
            response_path,
            media_type="audio/wav",
            filename="voice-sample.wav",
            background=BackgroundTask(response_path.unlink, missing_ok=True),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Audio extraction took too long. Try a shorter or smaller source file.")
    finally:
        if source_path:
            source_path.unlink(missing_ok=True)
        if output_path:
            output_path.unlink(missing_ok=True)


YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_EXTRACT_MAX_SECONDS = 29.0
YOUTUBE_EXTRACT_MAX_BYTES = 250 * 1024 * 1024
YOUTUBE_EXTRACT_SEMAPHORE = asyncio.Semaphore(1)


def _youtube_video_id(value: str) -> str | None:
    """Accept a single ordinary YouTube video URL and return only its video ID."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return None
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = [part for part in parsed.path.split("/") if part]
        if host == "youtu.be":
            candidate = path[0] if path else ""
        elif host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
            if path and path[0] in {"shorts", "embed", "live"}:
                candidate = path[1] if len(path) > 1 else ""
            else:
                candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            return None
        return candidate if YOUTUBE_VIDEO_ID.fullmatch(candidate) else None
    except ValueError:
        return None


def _download_youtube_sample(video_id: str, start_seconds: float, end_seconds: float, yt_dlp: str, ffmpeg: str) -> tuple[Path, Path]:
    """Download the selected public clip to disk, normalize to WAV, and return cleanup paths."""
    temp_root = config.get_data_dir() / "tmp" / "youtube"
    temp_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="clip-", dir=temp_root))
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = str(work_dir / "source.%(ext)s")
    section = f"*{start_seconds:.3f}-{end_seconds:.3f}"
    command = [
        yt_dlp,
        "--ignore-config", "--no-playlist", "--no-progress", "--no-warnings",
        "--no-write-info-json", "--no-write-thumbnail", "--no-write-subs", "--no-write-auto-subs",
        "--js-runtimes", "deno",
        "--format", "bestaudio/best", "--download-sections", section,
        "--force-keyframes-at-cuts", "--max-filesize", "250M",
        "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
        "--output", output_template, canonical_url,
    ]
    try:
        free_bytes = shutil.disk_usage(temp_root).free
        if free_bytes < YOUTUBE_EXTRACT_MAX_BYTES + 50 * 1024 * 1024:
            raise HTTPException(status_code=507, detail="Voicebox storage is too low for temporary clip extraction.")
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            logger.info("YouTube audio extraction failed for video ID %s", video_id)
            raise HTTPException(
                status_code=422,
                detail="Voicebox could not retrieve audio for this video. It may be unavailable or block extraction; try an authorised source file instead.",
            )

        candidates = [
            path for path in work_dir.glob("source.*")
            if path.is_file() and not path.name.endswith((".part", ".ytdl", ".json", ".jpg", ".webp", ".vtt"))
        ]
        if not candidates:
            raise HTTPException(status_code=422, detail="No audio was returned for the selected section.")
        source_path = max(candidates, key=lambda path: path.stat().st_size)
        if source_path.stat().st_size > YOUTUBE_EXTRACT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="The selected audio segment exceeded the temporary download limit.")

        output_path = work_dir / "voice-sample.wav"
        normalize_command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source_path), "-t", f"{end_seconds - start_seconds:.3f}",
            "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(output_path),
        ]
        normalized = subprocess.run(normalize_command, capture_output=True, text=True, timeout=45, check=False)
        if normalized.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 44:
            raise HTTPException(status_code=422, detail="Voicebox could not convert the selected section into a voice sample.")
        return output_path, work_dir
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=408, detail="YouTube audio extraction took too long. Try another video or use an authorised source file.") from exc
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


@router.post("/profiles/extract-youtube-sample")
async def extract_youtube_profile_sample(
    video_url: str = Form(...),
    start_seconds: float = Form(...),
    end_seconds: float = Form(...),
    authorized: bool = Form(...),
):
    """Extract one authorised public YouTube clip; temporary source media is deleted after response."""
    from ..utils.ffmpeg import find_ffmpeg

    video_id = _youtube_video_id(video_url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Enter a valid YouTube video URL.")
    if not authorized:
        raise HTTPException(status_code=403, detail="Confirm you have permission to use this media and the speaker's voice.")
    if not math.isfinite(start_seconds) or not math.isfinite(end_seconds):
        raise HTTPException(status_code=400, detail="Clip start and end must be valid times.")
    clip_duration = end_seconds - start_seconds
    if start_seconds < 0 or clip_duration <= 0 or clip_duration > YOUTUBE_EXTRACT_MAX_SECONDS:
        raise HTTPException(status_code=400, detail="Choose a clip up to 29 seconds long.")

    yt_dlp = shutil.which("yt-dlp")
    ffmpeg = find_ffmpeg()
    if not yt_dlp or not ffmpeg:
        raise HTTPException(status_code=503, detail="YouTube clip extraction is not configured on this Voicebox server.")

    try:
        await asyncio.wait_for(YOUTUBE_EXTRACT_SEMAPHORE.acquire(), timeout=5)
    except TimeoutError as exc:
        raise HTTPException(status_code=429, detail="Another YouTube clip is being extracted. Try again shortly.") from exc
    try:
        output_path, work_dir = await asyncio.to_thread(
            _download_youtube_sample, video_id, start_seconds, end_seconds, yt_dlp, ffmpeg
        )
    finally:
        YOUTUBE_EXTRACT_SEMAPHORE.release()
    return FileResponse(
        output_path,
        media_type="audio/wav",
        filename="youtube-voice-sample.wav",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
    )


@router.post("/profiles", response_model=models.VoiceProfileResponse)
async def create_profile(
    data: models.VoiceProfileCreate,
    db: Session = Depends(get_db),
):
    """Create a new voice profile."""
    try:
        return await profiles.create_profile(data, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/profiles", response_model=list[models.VoiceProfileResponse])
async def list_profiles(db: Session = Depends(get_db)):
    """List all voice profiles."""
    return await profiles.list_profiles(db)


@router.post("/profiles/import", response_model=models.VoiceProfileResponse)
async def import_profile(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Import a voice profile from a ZIP archive."""
    MAX_FILE_SIZE = 100 * 1024 * 1024

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400, detail=f"File too large. Maximum size is {MAX_FILE_SIZE / (1024 * 1024)}MB"
        )

    try:
        profile = await export_import.import_profile_from_zip(content, db)
        return profile
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Preset Voice Endpoints ───────────────────────────────────────────
# These MUST be declared before /profiles/{profile_id} to avoid the
# wildcard swallowing "presets" as a profile_id.


@router.get("/profiles/presets/{engine}")
async def list_preset_voices(engine: str):
    """List available preset voices for an engine."""
    if engine == "kokoro":
        from ..backends.kokoro_backend import KOKORO_VOICES

        return {
            "engine": engine,
            "voices": [
                {
                    "voice_id": vid,
                    "name": name,
                    "gender": gender,
                    "language": lang,
                }
                for vid, name, gender, lang in KOKORO_VOICES
            ],
        }
    if engine == "qwen_custom_voice":
        from ..backends.qwen_custom_voice_backend import QWEN_CUSTOM_VOICES

        return {
            "engine": engine,
            "voices": [
                {
                    "voice_id": speaker_id,
                    "name": display_name,
                    "gender": gender,
                    "language": lang,
                }
                for speaker_id, display_name, gender, lang, _desc in QWEN_CUSTOM_VOICES
            ],
        }
    return {"engine": engine, "voices": []}

@router.get("/profiles/{profile_id}", response_model=models.VoiceProfileResponse)
async def get_profile(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Get a voice profile by ID."""
    profile = await profiles.get_profile(profile_id, db)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.put("/profiles/{profile_id}", response_model=models.VoiceProfileResponse)
async def update_profile(
    profile_id: str,
    data: models.VoiceProfileCreate,
    db: Session = Depends(get_db),
):
    """Update a voice profile."""
    try:
        profile = await profiles.update_profile(profile_id, data, db)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        return profile
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Delete a voice profile."""
    success = await profiles.delete_profile(profile_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"message": "Profile deleted successfully"}


SAMPLE_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
SAMPLE_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB


@router.post("/profiles/{profile_id}/samples", response_model=models.ProfileSampleResponse)
async def add_profile_sample(
    profile_id: str,
    file: UploadFile = File(...),
    reference_text: str = Form(...),
    db: Session = Depends(get_db),
):
    """Add a sample to a voice profile."""
    _allowed_audio_exts = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm", ".opus"}
    _uploaded_ext = Path(file.filename or "").suffix.lower()
    file_suffix = _uploaded_ext if _uploaded_ext in _allowed_audio_exts else ".wav"

    with tempfile.NamedTemporaryFile(suffix=file_suffix, delete=False) as tmp:
        total_size = 0
        while chunk := await file.read(SAMPLE_UPLOAD_CHUNK_SIZE):
            total_size += len(chunk)
            if total_size > SAMPLE_MAX_FILE_SIZE:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (max {SAMPLE_MAX_FILE_SIZE // (1024 * 1024)} MB)",
                )
            tmp.write(chunk)
        tmp_path = tmp.name

    try:
        sample = await profiles.add_profile_sample(
            profile_id,
            tmp_path,
            reference_text,
            db,
        )
        return sample
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process audio file: {str(e)}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.get("/profiles/{profile_id}/samples", response_model=list[models.ProfileSampleResponse])
async def get_profile_samples(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Get all samples for a profile."""
    return await profiles.get_profile_samples(profile_id, db)


@router.delete("/profiles/samples/{sample_id}")
async def delete_profile_sample(
    sample_id: str,
    db: Session = Depends(get_db),
):
    """Delete a profile sample."""
    success = await profiles.delete_profile_sample(sample_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Sample not found")
    return {"message": "Sample deleted successfully"}


@router.put("/profiles/samples/{sample_id}", response_model=models.ProfileSampleResponse)
async def update_profile_sample(
    sample_id: str,
    data: models.ProfileSampleUpdate,
    db: Session = Depends(get_db),
):
    """Update a profile sample's reference text."""
    sample = await profiles.update_profile_sample(sample_id, data.reference_text, db)
    if not sample:
        raise HTTPException(status_code=404, detail="Sample not found")
    return sample


@router.post("/profiles/{profile_id}/avatar", response_model=models.VoiceProfileResponse)
async def upload_profile_avatar(
    profile_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload or update avatar image for a profile."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "").suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        profile = await profiles.upload_avatar(profile_id, tmp_path, db)
        return profile
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.get("/profiles/{profile_id}/avatar")
async def get_profile_avatar(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Get avatar image for a profile."""
    profile = await profiles.get_profile(profile_id, db)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if not profile.avatar_path:
        raise HTTPException(status_code=404, detail="No avatar found for this profile")

    avatar_path = config.resolve_storage_path(profile.avatar_path)
    if avatar_path is None or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar file not found")

    return FileResponse(avatar_path)


@router.delete("/profiles/{profile_id}/avatar")
async def delete_profile_avatar(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Delete avatar image for a profile."""
    success = await profiles.delete_avatar(profile_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Profile not found or no avatar to delete")
    return {"message": "Avatar deleted successfully"}


@router.get("/profiles/{profile_id}/export")
async def export_profile(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Export a voice profile as a ZIP archive."""
    try:
        profile = await profiles.get_profile(profile_id, db)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")

        zip_bytes = export_import.export_profile_to_zip(profile_id, db)

        safe_name = "".join(c for c in profile.name if c.isalnum() or c in (" ", "-", "_")).strip()
        if not safe_name:
            safe_name = "profile"
        filename = f"profile-{safe_name}.voicebox.zip"

        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={"Content-Disposition": safe_content_disposition("attachment", filename)},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profiles/{profile_id}/channels")
async def get_profile_channels(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Get list of channel IDs assigned to a profile."""
    try:
        channel_ids = await channels.get_profile_channels(profile_id, db)
        return {"channel_ids": channel_ids}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/profiles/{profile_id}/channels")
async def set_profile_channels(
    profile_id: str,
    data: models.ProfileChannelAssignment,
    db: Session = Depends(get_db),
):
    """Set which channels a profile is assigned to."""
    try:
        await channels.set_profile_channels(profile_id, data, db)
        return {"message": "Profile channels updated successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/profiles/{profile_id}/effects", response_model=models.VoiceProfileResponse)
async def update_profile_effects(
    profile_id: str,
    data: models.ProfileEffectsUpdate,
    db: Session = Depends(get_db),
):
    """Set or clear the default effects chain for a voice profile."""
    profile = db.query(DBVoiceProfile).filter_by(id=profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    if data.effects_chain is not None:
        from ..utils.effects import validate_effects_chain

        chain_dicts = [e.model_dump() for e in data.effects_chain]
        error = validate_effects_chain(chain_dicts)
        if error:
            raise HTTPException(status_code=400, detail=error)
        profile.effects_chain = _json.dumps(chain_dicts)
    else:
        profile.effects_chain = None

    profile.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(profile)

    return _profile_to_response(profile)


# ── Personality endpoint ──────────────────────────────────────────────
# Only ``/profiles/{id}/compose`` remains — the UI's compose button
# produces a fresh in-character utterance the user can edit before
# speaking. Rewrite now happens inside ``/generate`` (and ``/speak``)
# when ``personality=true``; there is no standalone rewrite/respond/speak
# endpoint.


@router.post(
    "/profiles/{profile_id}/compose",
    response_model=models.PersonalityTextResponse,
)
async def compose_in_character(
    profile_id: str,
    db: Session = Depends(get_db),
):
    """Produce a fresh utterance in the profile's character voice."""
    profile = db.query(DBVoiceProfile).filter_by(id=profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    try:
        result = await personality.compose_as_profile(profile.personality)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return models.PersonalityTextResponse(
        text=result.text, model_size=result.model_size
    )
