from pathlib import Path

import pytest

from core.adobe_client import AdobeClient
from core.video_generation import generate_video_file


class FakeVideoClient:
    def __init__(
        self,
        *,
        payload: bytes = b"video",
        content_type: str = "video/mp4",
        fail: bool = False,
    ) -> None:
        self.payload = payload
        self.content_type = content_type
        self.fail = fail
        self.calls: list[dict] = []

    def generate_video(self, **kwargs):
        self.calls.append(kwargs)
        out_path = Path(kwargs["out_path"])
        out_path.write_bytes(b"partial" if self.fail else self.payload)
        if self.fail:
            raise RuntimeError("generation failed")
        return None, {"contentType": self.content_type}


def _generate(
    tmp_path: Path,
    client: FakeVideoClient,
    accounted: list[tuple[Path, int, int]],
):
    return generate_video_file(
        client=client,
        token="token",
        video_conf={"engine": "sora2"},
        prompt="a test video",
        aspect_ratio="16:9",
        duration=4,
        generated_dir=tmp_path,
        task_id="job",
        resolution="720p",
        negative_prompt="",
        generate_audio=True,
        source_image_ids=[],
        entity_refs=None,
        reference_mode="frame",
        timeout=600,
        progress_cb=None,
        on_generated_file_written=lambda path, old, new: accounted.append(
            (path, old, new)
        ),
    )


def test_generate_video_file_uses_real_mime_extension_and_accounts(tmp_path: Path):
    client = FakeVideoClient(payload=b"webm", content_type="video/webm")
    accounted: list[tuple[Path, int, int]] = []

    result = _generate(tmp_path, client, accounted)

    assert result.path == tmp_path / "job.webm"
    assert result.mime_type == "video/webm"
    assert result.metadata == {"contentType": "video/webm"}
    assert result.path.read_bytes() == b"webm"
    assert accounted == [(result.path, 0, 4)]
    assert not (tmp_path / "job.video.tmp").exists()


def test_generate_video_file_removes_partial_temp_on_failure(tmp_path: Path):
    client = FakeVideoClient(fail=True)

    with pytest.raises(RuntimeError, match="generation failed"):
        _generate(tmp_path, client, [])

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("content_type", "extension", "mime_type"),
    [
        ("video/mp4", "mp4", "video/mp4"),
        ("video/ogg", "ogv", "video/ogg"),
        ("application/octet-stream", "mp4", "video/mp4"),
    ],
)
def test_generate_video_file_normalizes_supported_video_mime_types(
    tmp_path: Path,
    content_type: str,
    extension: str,
    mime_type: str,
):
    result = _generate(
        tmp_path,
        FakeVideoClient(content_type=content_type),
        [],
    )

    assert result.path.suffix == f".{extension}"
    assert result.mime_type == mime_type


def test_veo_payload_includes_nonempty_negative_prompt_only():
    client = AdobeClient()
    base_args = {
        "video_conf": {"engine": "veo31-standard"},
        "prompt": "main prompt",
        "aspect_ratio": "16:9",
        "duration": 8,
    }

    with_negative = client._build_video_payload(
        **base_args,
        negative_prompt="no text overlays",
    )
    without_negative = client._build_video_payload(
        **base_args,
        negative_prompt="",
    )

    assert with_negative["modelSpecificPayload"]["parameters"][
        "negativePrompt"
    ] == "no text overlays"
    assert "negativePrompt" not in without_negative["modelSpecificPayload"][
        "parameters"
    ]
