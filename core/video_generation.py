from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class GeneratedVideoFile:
    path: Path
    mime_type: str
    metadata: dict


def video_extension_and_mime(metadata: dict) -> tuple[str, str]:
    content_type = str((metadata or {}).get("contentType") or "video/mp4").lower()
    if "webm" in content_type:
        return "webm", "video/webm"
    if "ogg" in content_type or "ogv" in content_type:
        return "ogv", "video/ogg"
    return "mp4", "video/mp4"


def generate_video_file(
    *,
    client: Any,
    token: str,
    video_conf: dict,
    prompt: str,
    aspect_ratio: str,
    duration: int,
    generated_dir: Path,
    task_id: str,
    resolution: str,
    negative_prompt: str,
    generate_audio: bool,
    source_image_ids: list[str],
    entity_refs: Optional[list[dict]],
    reference_mode: str,
    timeout: int,
    progress_cb: Optional[Callable[[dict], None]],
    on_generated_file_written: Callable[[Path, int, int], None],
) -> GeneratedVideoFile:
    generated_dir.mkdir(parents=True, exist_ok=True)
    temp_path = generated_dir / f"{task_id}.video.tmp"
    final_path: Path | None = None
    final_created = False
    temp_path.unlink(missing_ok=True)

    effective_conf = dict(video_conf or {})
    effective_conf["resolution"] = str(resolution or "720p")

    try:
        video_bytes, metadata = client.generate_video(
            token=token,
            video_conf=effective_conf,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            duration=int(duration),
            source_image_ids=list(source_image_ids or []),
            entity_refs=entity_refs,
            timeout=int(timeout),
            negative_prompt=str(negative_prompt or ""),
            generate_audio=bool(generate_audio),
            reference_mode=str(reference_mode or "frame"),
            out_path=temp_path,
            progress_cb=progress_cb,
        )
        if video_bytes is not None:
            temp_path.write_bytes(video_bytes)
        if not temp_path.exists() or not temp_path.is_file():
            raise RuntimeError("video generation completed without an output file")

        metadata = dict(metadata or {})
        extension, mime_type = video_extension_and_mime(metadata)
        final_path = generated_dir / f"{task_id}.{extension}"
        old_size = int(final_path.stat().st_size) if final_path.exists() else 0
        temp_path.replace(final_path)
        final_created = True
        new_size = int(final_path.stat().st_size)
        on_generated_file_written(final_path, old_size, new_size)
        return GeneratedVideoFile(
            path=final_path,
            mime_type=mime_type,
            metadata=metadata,
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        if final_created and final_path is not None:
            final_path.unlink(missing_ok=True)
        raise
