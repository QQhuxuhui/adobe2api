from pathlib import Path

from core.image_generation import generate_image_artifact


class ReturningClient:
    generate_timeout = 60
    gpt_image_quality = "low"

    def __init__(self):
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return b"returned-image", {"progress": 100}


class FileWritingClient(ReturningClient):
    def generate(self, **kwargs):
        self.kwargs = kwargs
        kwargs["out_path"].write_bytes(b"file-image")
        return None, {"progress": 100}


def _model_config():
    return {
        "upstream_model_id": "gpt-image",
        "upstream_model_version": "2",
        "detail_level": "high",
    }


def test_executor_writes_returned_bytes_and_passes_adobe_options(tmp_path: Path):
    client = ReturningClient()
    writes = []
    artifact = generate_image_artifact(
        client=client,
        token="token",
        prompt="draw",
        aspect_ratio="1:1",
        output_resolution="1K",
        model_config=_model_config(),
        generated_dir=tmp_path,
        source_image_ids=["source-1"],
        progress_cb=lambda update: None,
        on_generated_file_written=lambda path, old, new: writes.append((path, old, new)),
        job_id="fixed",
    )
    assert artifact.image_bytes == b"returned-image"
    assert artifact.path.read_bytes() == b"returned-image"
    assert client.kwargs["quality_level"] == "low"
    assert client.kwargs["source_image_ids"] == ["source-1"]
    assert writes == [(tmp_path / "fixed.png", 0, len(b"returned-image"))]


def test_executor_reads_file_when_client_streams_to_out_path(tmp_path: Path):
    artifact = generate_image_artifact(
        client=FileWritingClient(),
        token="token",
        prompt="draw",
        aspect_ratio="1:1",
        output_resolution="1K",
        model_config=_model_config(),
        generated_dir=tmp_path,
        source_image_ids=[],
        progress_cb=None,
        on_generated_file_written=lambda path, old, new: None,
        job_id="fixed",
    )
    assert artifact.image_bytes == b"file-image"
