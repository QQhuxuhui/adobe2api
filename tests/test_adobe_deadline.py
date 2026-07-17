from pathlib import Path
import sys
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.adobe_client as adobe_client_module
from core.adobe_client import AdobeClient, AdobeRequestError, UpstreamTemporaryError


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        content: bytes = b"image",
        chunks: Optional[list[bytes]] = None,
    ) -> None:
        self.status_code = status_code
        self._data = data or {}
        self.headers = headers or {}
        self.content = content
        self.text = ""
        self._chunks = chunks or []

    def json(self) -> dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, exc, traceback
        return False


def make_client() -> AdobeClient:
    client = AdobeClient.__new__(AdobeClient)
    client.api_key = "test-key"
    client.impersonate = "chrome124"
    client.proxy = ""
    client.user_agent = "test-agent"
    client.sec_ch_ua = '"Chromium";v="124"'
    client.gpt_image_quality = "low"
    return client


def test_requests_fallback_crops_each_network_timeout(monkeypatch, tmp_path: Path):
    client = make_client()
    captured: list[float] = []

    monkeypatch.setattr(adobe_client_module, "CurlSession", None)

    def fake_post(*args, **kwargs):
        del args
        captured.append(kwargs["timeout"])
        return FakeResponse()

    def fake_get(*args, **kwargs):
        del args
        captured.append(kwargs["timeout"])
        return FakeResponse(chunks=[b"data"])

    monkeypatch.setattr(adobe_client_module.requests, "post", fake_post)
    monkeypatch.setattr(adobe_client_module.requests, "get", fake_get)

    deadline = adobe_client_module.time.monotonic() + 0.25
    client._post_json("https://example.test", {}, {}, deadline=deadline)
    client._post_bytes("https://example.test", {}, b"data", deadline=deadline)
    client._get("https://example.test", {}, timeout=30, deadline=deadline)
    client._download_to_file(
        "https://example.test",
        {},
        tmp_path / "image.png",
        timeout=30,
        deadline=deadline,
    )

    assert len(captured) == 4
    assert all(0 < timeout <= 0.25 for timeout in captured)


def test_deadline_none_preserves_fixed_requests_timeouts(monkeypatch, tmp_path: Path):
    client = make_client()
    captured: list[float] = []

    monkeypatch.setattr(adobe_client_module, "CurlSession", None)

    def fake_post(*args, **kwargs):
        del args
        captured.append(kwargs["timeout"])
        return FakeResponse()

    def fake_get(*args, **kwargs):
        del args
        captured.append(kwargs["timeout"])
        return FakeResponse(chunks=[b"data"])

    monkeypatch.setattr(adobe_client_module.requests, "post", fake_post)
    monkeypatch.setattr(adobe_client_module.requests, "get", fake_get)

    client._post_json("https://example.test", {}, {}, deadline=None)
    client._post_bytes("https://example.test", {}, b"data", deadline=None)
    client._get("https://example.test", {}, timeout=30, deadline=None)
    client._download_to_file(
        "https://example.test",
        {},
        tmp_path / "image.png",
        timeout=30,
        deadline=None,
    )

    assert captured == [60.0, 60.0, 30.0, 30.0]


@pytest.mark.parametrize(
    "invoke",
    [
        lambda client, deadline, path: client._post_json(
            "https://example.test", {}, {}, deadline=deadline
        ),
        lambda client, deadline, path: client._post_bytes(
            "https://example.test", {}, b"data", deadline=deadline
        ),
        lambda client, deadline, path: client._get(
            "https://example.test", {}, deadline=deadline
        ),
        lambda client, deadline, path: client._download_to_file(
            "https://example.test", {}, path, deadline=deadline
        ),
    ],
)
def test_expired_deadline_fails_before_requests(
    monkeypatch, tmp_path: Path, invoke
):
    client = make_client()
    called = False

    monkeypatch.setattr(adobe_client_module, "CurlSession", None)

    def fail_network_call(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    monkeypatch.setattr(adobe_client_module.requests, "post", fail_network_call)
    monkeypatch.setattr(adobe_client_module.requests, "get", fail_network_call)

    with pytest.raises(UpstreamTemporaryError) as exc_info:
        invoke(client, adobe_client_module.time.monotonic() - 1.0, tmp_path / "out")

    assert exc_info.value.error_type == "timeout"
    assert exc_info.value.status_code == 503
    assert called is False


def test_curl_session_constructor_receives_cropped_timeout(monkeypatch):
    client = make_client()
    captured: dict[str, Any] = {}

    class FakeCurlSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> bool:
            del exc_type, exc, traceback
            return False

        def post(self, url, headers, json):
            del url, headers, json
            return FakeResponse()

    monkeypatch.setattr(adobe_client_module, "CurlSession", FakeCurlSession)

    deadline = adobe_client_module.time.monotonic() + 0.25
    client._post_json("https://example.test", {}, {}, deadline=deadline)

    assert captured["impersonate"] == "chrome124"
    assert 0 < captured["timeout"] <= 0.25


def test_deadline_none_preserves_fixed_curl_session_timeout(monkeypatch):
    client = make_client()
    captured: dict[str, Any] = {}

    class FakeCurlSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(adobe_client_module, "CurlSession", FakeCurlSession)

    client._session(timeout=30, deadline=None)

    assert captured["timeout"] == 30.0


def test_expired_deadline_fails_before_curl_session_construction(monkeypatch):
    client = make_client()
    constructed = False

    class FakeCurlSession:
        def __init__(self, **kwargs) -> None:
            del kwargs
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(adobe_client_module, "CurlSession", FakeCurlSession)

    with pytest.raises(UpstreamTemporaryError) as exc_info:
        client._post_json(
            "https://example.test",
            {},
            {},
            deadline=adobe_client_module.time.monotonic() - 1.0,
        )

    assert exc_info.value.error_type == "timeout"
    assert constructed is False


def test_status_451_requests_fallback_recomputes_remaining_timeout(monkeypatch):
    client = make_client()
    curl_timeout: list[float] = []
    requests_timeout: list[float] = []
    clock = iter([100.0, 100.4])

    class FakeCurlSession:
        def __init__(self, **kwargs) -> None:
            curl_timeout.append(kwargs["timeout"])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> bool:
            del exc_type, exc, traceback
            return False

        def post(self, url, headers, json):
            del url, headers, json
            return FakeResponse(status_code=451)

    def fake_post(*args, **kwargs):
        del args
        requests_timeout.append(kwargs["timeout"])
        return FakeResponse()

    monkeypatch.setattr(adobe_client_module, "CurlSession", FakeCurlSession)
    monkeypatch.setattr(adobe_client_module.requests, "post", fake_post)
    monkeypatch.setattr(adobe_client_module.time, "monotonic", lambda: next(clock))

    client._post_json("https://example.test", {}, {}, deadline=101.0)

    assert curl_timeout == [1.0]
    assert requests_timeout == [pytest.approx(0.6)]


def test_streaming_download_checks_deadline_before_each_chunk(monkeypatch, tmp_path: Path):
    client = make_client()
    clock = iter([100.0, 100.1, 101.1])

    monkeypatch.setattr(adobe_client_module, "CurlSession", None)
    monkeypatch.setattr(
        adobe_client_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(chunks=[b"first", b"second"]),
    )
    monkeypatch.setattr(adobe_client_module.time, "monotonic", lambda: next(clock))

    out_path = tmp_path / "partial.png"
    with pytest.raises(UpstreamTemporaryError) as exc_info:
        client._download_to_file(
            "https://example.test", {}, out_path, deadline=101.0
        )

    assert exc_info.value.error_type == "timeout"
    assert out_path.read_bytes() == b"first"


def test_upload_image_forwards_deadline(monkeypatch):
    client = make_client()
    captured: list[Optional[float]] = []

    def fake_post_bytes(url, headers, payload, deadline=None):
        del url, headers, payload
        captured.append(deadline)
        return FakeResponse(data={"images": [{"id": "image-id"}]})

    monkeypatch.setattr(client, "_post_bytes", fake_post_bytes)

    deadline = 123.5
    assert client.upload_image("token", b"image", deadline=deadline) == "image-id"
    assert captured == [deadline]


def configure_generate_stubs(
    monkeypatch,
    client: AdobeClient,
) -> None:
    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kwargs: [{}])
    monkeypatch.setattr(
        client,
        "_extract_result_link",
        lambda response, data: "https://example.test/jobs/job-id",
    )


def test_generate_forwards_deadline_to_submit_poll_and_file_download(
    monkeypatch, tmp_path: Path
):
    client = make_client()
    calls: list[tuple[str, Optional[float]]] = []
    deadline = adobe_client_module.time.monotonic() + 100.0
    poll_data = {
        "outputs": [{"image": {"presignedUrl": "https://example.test/image"}}]
    }
    poll_responses = iter(
        [FakeResponse(data={"status": "RUNNING"}), FakeResponse(data=poll_data)]
    )
    configure_generate_stubs(monkeypatch, client)

    def fake_post_json(url, headers, payload, deadline=None):
        del url, headers, payload
        calls.append(("submit", deadline))
        return FakeResponse(data={})

    def fake_get(url, headers, timeout=60, deadline=None):
        del url, headers, timeout
        calls.append(("poll", deadline))
        return next(poll_responses)

    def fake_download(
        url,
        headers,
        out_path,
        timeout=60,
        chunk_size=1024 * 1024,
        deadline=None,
    ):
        del url, headers, out_path, timeout, chunk_size
        calls.append(("download", deadline))
        return 5

    monkeypatch.setattr(client, "_post_json", fake_post_json)
    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "_download_to_file", fake_download)
    monkeypatch.setattr(adobe_client_module.time, "sleep", lambda seconds: None)

    image_bytes, _ = client.generate(
        "token", "prompt", out_path=tmp_path / "image.png", deadline=deadline
    )

    assert image_bytes is None
    assert calls == [
        ("submit", deadline),
        ("poll", deadline),
        ("poll", deadline),
        ("download", deadline),
    ]


def test_generate_forwards_deadline_to_memory_download(monkeypatch):
    client = make_client()
    calls: list[tuple[str, Optional[float], int]] = []
    deadline = adobe_client_module.time.monotonic() + 100.0
    poll_data = {
        "outputs": [{"image": {"presignedUrl": "https://example.test/image"}}]
    }
    configure_generate_stubs(monkeypatch, client)

    monkeypatch.setattr(
        client,
        "_post_json",
        lambda url, headers, payload, deadline=None: FakeResponse(data={}),
    )

    def fake_get(url, headers, timeout=60, deadline=None):
        del headers
        calls.append((url, deadline, timeout))
        if url.endswith("job-id"):
            return FakeResponse(data=poll_data)
        return FakeResponse(content=b"image-bytes")

    monkeypatch.setattr(client, "_get", fake_get)

    image_bytes, _ = client.generate("token", "prompt", deadline=deadline)

    assert image_bytes == b"image-bytes"
    assert calls == [
        ("https://example.test/jobs/job-id", deadline, 60),
        ("https://example.test/image", deadline, 30),
    ]


def test_generate_with_expired_deadline_raises_temporary_error_before_network(
    monkeypatch,
):
    client = make_client()
    network_called = False

    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kwargs: [{}])
    monkeypatch.setattr(adobe_client_module, "CurlSession", None)

    def fail_post(*args, **kwargs):
        del args, kwargs
        nonlocal network_called
        network_called = True
        raise AssertionError("network must not be called")

    monkeypatch.setattr(adobe_client_module.requests, "post", fail_post)

    with pytest.raises(UpstreamTemporaryError) as exc_info:
        client.generate(
            "token",
            "prompt",
            deadline=adobe_client_module.time.monotonic() - 1.0,
        )

    assert exc_info.value.error_type == "timeout"
    assert network_called is False


def test_generate_without_deadline_keeps_local_timeout_error(monkeypatch):
    client = make_client()
    clock = iter([0.0, 2.0])
    configure_generate_stubs(monkeypatch, client)
    monkeypatch.setattr(
        client,
        "_post_json",
        lambda url, headers, payload, deadline=None: FakeResponse(data={}),
    )
    monkeypatch.setattr(
        client,
        "_get",
        lambda url, headers, timeout=60, deadline=None: FakeResponse(
            data={"status": "RUNNING"}
        ),
    )
    monkeypatch.setattr(adobe_client_module.time, "monotonic", lambda: next(clock))

    with pytest.raises(AdobeRequestError, match="generation timed out") as exc_info:
        client.generate("token", "prompt", timeout=1, deadline=None)

    assert not isinstance(exc_info.value, UpstreamTemporaryError)


def test_generate_poll_sleep_is_capped_by_deadline(monkeypatch):
    client = make_client()
    clock = iter([0.0, 9.8, 9.8, 9.8])
    sleeps: list[float] = []
    polls = iter(
        [
            FakeResponse(data={"status": "RUNNING"}),
            FakeResponse(
                data={
                    "outputs": [
                        {"image": {"presignedUrl": "https://example.test/image"}}
                    ]
                }
            ),
            FakeResponse(content=b"image"),
        ]
    )
    configure_generate_stubs(monkeypatch, client)
    monkeypatch.setattr(
        client,
        "_post_json",
        lambda url, headers, payload, deadline=None: FakeResponse(data={}),
    )
    monkeypatch.setattr(
        client,
        "_get",
        lambda url, headers, timeout=60, deadline=None: next(polls),
    )
    monkeypatch.setattr(adobe_client_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(adobe_client_module.time, "sleep", sleeps.append)

    image_bytes, _ = client.generate("token", "prompt", timeout=100, deadline=10.0)

    assert image_bytes == b"image"
    assert sleeps == [pytest.approx(0.2)]
