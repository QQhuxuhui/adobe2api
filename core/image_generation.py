"""Shared Adobe image generation and artifact persistence helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class GeneratedImageArtifact:
    job_id: str
    path: Path
    image_bytes: bytes
    metadata: dict[str, Any]


def generate_image_artifact(
    *,
    client,
    token: str,
    prompt: str,
    aspect_ratio: str,
    output_resolution: str,
    model_config: Mapping[str, Any],
    generated_dir: Path,
    source_image_ids: Sequence[str],
    output_size: Mapping[str, int] | None = None,
    fallback_aspect_ratio: str | None = None,
    progress_cb: Callable[[dict], None] | None,
    on_generated_file_written: Callable[[Path, int, int], None],
    job_id: str | None = None,
) -> GeneratedImageArtifact:
    resolved_job_id = job_id or uuid.uuid4().hex
    path = generated_dir / f"{resolved_job_id}.png"
    try:
        old_size = int(path.stat().st_size) if path.exists() else 0
    except OSError:
        old_size = 0

    image_bytes, metadata = client.generate(
        token=token,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        output_resolution=output_resolution,
        upstream_model_id=str(
            model_config.get("upstream_model_id") or "gemini-flash"
        ),
        upstream_model_version=str(
            model_config.get("upstream_model_version") or "nano-banana-2"
        ),
        quality_level=(
            client.gpt_image_quality
            if str(model_config.get("upstream_model_id") or "") == "gpt-image"
            else None
        ),
        detail_level=model_config.get("detail_level"),
        source_image_ids=list(source_image_ids),
        output_size=dict(output_size) if output_size is not None else None,
        fallback_aspect_ratio=fallback_aspect_ratio,
        timeout=client.generate_timeout,
        out_path=path,
        progress_cb=progress_cb,
    )
    if image_bytes is not None:
        path.write_bytes(image_bytes)
    final_bytes = path.read_bytes()
    on_generated_file_written(path, old_size, len(final_bytes))
    return GeneratedImageArtifact(
        job_id=resolved_job_id,
        path=path,
        image_bytes=final_bytes,
        metadata=dict(metadata or {}),
    )
