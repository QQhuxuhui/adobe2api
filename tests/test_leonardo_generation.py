import pytest

from core.leonardo_generation import to_aspect, clamp_quantity, generate_images
from core.leonardo_client import LeonardoError


def test_to_aspect_prefers_explicit_aspect_ratio():
    assert to_aspect(size="1024x1024", aspect_ratio="16:9") == "16:9"


def test_to_aspect_from_size():
    assert to_aspect(size="1792x1024") == "16:9"
    assert to_aspect(size="1024x1792") == "9:16"
    assert to_aspect(size="1024x1024") == "1:1"


def test_to_aspect_passthrough_supported_ratio():
    assert to_aspect(aspect_ratio="4:3") == "4:3"


def test_to_aspect_defaults_to_square_on_unknown():
    assert to_aspect() == "1:1"
    assert to_aspect(size="weird") == "1:1"
    assert to_aspect(aspect_ratio="7:5") == "1:1"   # 不在支持集


def test_clamp_quantity():
    assert clamp_quantity(1) == 1
    assert clamp_quantity(9) == 4
    assert clamp_quantity(0) == 1
    assert clamp_quantity(None) == 1
    assert clamp_quantity("3") == 3
    assert clamp_quantity("x") == 1


class _FakeClient:
    def __init__(self, *, gen_id="gen-1", result=None, credits=8500):
        self._gen_id = gen_id
        self._result = result or {"success": True, "images": ["https://cdn/a.jpg"]}
        self._credits = credits
        self.calls = {}

    def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1, init_image_ids=None):
        self.calls["create"] = dict(token=token, prompt=prompt, model_id=model_id,
                                    aspect_ratio=aspect_ratio, quantity=quantity)
        return self._gen_id

    def wait_for_completion(self, token, gen_id, **kwargs):
        self.calls["wait"] = dict(token=token, gen_id=gen_id, kwargs=kwargs)
        return self._result


def test_generate_images_happy_path():
    client = _FakeClient(gen_id="gen-9", result={"success": True, "images": ["https://cdn/x.jpg", "https://cdn/y.jpg"]})
    out = generate_images(client, "TOK", prompt="a cat", model_id="M1",
                          size="1792x1024", n=2, now=lambda: 1700000000)
    assert out["created"] == 1700000000
    assert out["data"] == [{"url": "https://cdn/x.jpg"}, {"url": "https://cdn/y.jpg"}]
    assert out["provider"] == {"generation_id": "gen-9", "aspect_ratio": "16:9", "model_id": "M1"}
    # 归一化正确传给 client
    assert client.calls["create"]["aspect_ratio"] == "16:9"
    assert client.calls["create"]["quantity"] == 2


def test_generate_images_requires_model_id():
    with pytest.raises(LeonardoError):
        generate_images(_FakeClient(), "TOK", prompt="x", model_id="")


def test_generate_images_raises_on_failed_result():
    client = _FakeClient(result={"success": False, "error": "generation failed"})
    with pytest.raises(LeonardoError):
        generate_images(client, "TOK", prompt="x", model_id="M1")


def test_generate_images_raises_generation_error_on_timeout():
    import pytest
    from core.leonardo_client import LeonardoError
    from core.leonardo_generation import LeonardoGenerationError, generate_images

    class _FakeClient:
        def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1):
            return "gen-1"

        def wait_for_completion(self, token, gen_id, *, timeout, poll_interval, sleep=None, now=None):
            return {"success": False, "error": "generation timeout"}

    with pytest.raises(LeonardoGenerationError) as excinfo:
        generate_images(
            _FakeClient(), "tok", prompt="p", model_id="m", timeout=5
        )
    # 与提交前失败同族，但类型可区分
    assert isinstance(excinfo.value, LeonardoError)


def test_generate_images_converts_wait_poll_raise_to_generation_error():
    """wait_for_completion 内轮询抛的 LeonardoError(mutation 已提交)必须转为不可重试。"""
    from core.leonardo_client import LeonardoError as LeonardoClientError
    from core.leonardo_generation import LeonardoGenerationError, generate_images

    class _FakeClient:
        def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1):
            return "gen-1"

        def wait_for_completion(self, token, gen_id, *, timeout, poll_interval, sleep=None, now=None):
            # 轮询查询 3 次传输重试耗尽; mutation 已提交, 不得换号重发
            raise LeonardoClientError(
                "graphql GetAIGenerationFeedStatuses failed after 3 attempts: connection reset"
            )

    with pytest.raises(LeonardoGenerationError) as excinfo:
        generate_images(_FakeClient(), "tok", prompt="p", model_id="m", timeout=5)
    assert isinstance(excinfo.value, LeonardoError)


def test_generate_images_passes_create_transport_unsafe_error_through():
    """create_generation 的单发传输失败(可能已受理)原样透传 LeonardoRetryUnsafeError。"""
    from core.leonardo_client import LeonardoRetryUnsafeError
    from core.leonardo_generation import generate_images

    class _FakeClient:
        def create_generation(self, token, prompt, model_id, aspect_ratio, quantity=1):
            raise LeonardoRetryUnsafeError(
                "graphql Generate failed; request not retried to avoid duplicate side effects: connection lost"
            )

        def wait_for_completion(self, token, gen_id, *, timeout, poll_interval, sleep=None, now=None):
            return {"success": True, "images": []}

    with pytest.raises(LeonardoRetryUnsafeError):
        generate_images(_FakeClient(), "tok", prompt="p", model_id="m", timeout=5)
