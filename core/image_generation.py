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
    quality_level: str | None = None,
    output_size: Mapping[str, int] | None = None,
    fallback_aspect_ratio: str | None = None,
    progress_cb: Callable[[dict], None] | None,
    on_generated_file_written: Callable[[Path, int, int], None],
    job_id: str | None = None,
    deadline: float | None = None,
) -> GeneratedImageArtifact:
    resolved_job_id = job_id or uuid.uuid4().hex
    path = generated_dir / f"{resolved_job_id}.png"
    try:
        old_size = int(path.stat().st_size) if path.exists() else 0
    except OSError:
        old_size = 0

    upstream_model_id = str(model_config.get("upstream_model_id") or "")
    is_gpt_image = upstream_model_id == "gpt-image"
    is_dynamic_gpt_image = is_gpt_image and bool(model_config.get("dynamic"))
    effective_quality = None
    if is_gpt_image:
        if is_dynamic_gpt_image and quality_level:
            effective_quality = quality_level
        else:
            effective_quality = client.gpt_image_quality

    image_bytes, metadata = client.generate(
        token=token,
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        output_resolution=output_resolution,
        upstream_model_id=str(
            upstream_model_id or "gemini-flash"
        ),
        upstream_model_version=str(
            model_config.get("upstream_model_version") or "nano-banana-2"
        ),
        quality_level=effective_quality,
        detail_level=model_config.get("detail_level"),
        source_image_ids=list(source_image_ids),
        output_size=dict(output_size) if output_size is not None else None,
        fallback_aspect_ratio=fallback_aspect_ratio,
        timeout=client.generate_timeout,
        out_path=path,
        progress_cb=progress_cb,
        deadline=deadline,
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
