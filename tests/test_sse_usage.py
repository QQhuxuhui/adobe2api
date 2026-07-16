"""离线单测: 流式 SSE 必须携带 usage(下游按 token 计费取自响应 usage)。

不依赖运行中的服务; 直接 python tests/test_sse_usage.py 或 pytest 均可。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.streaming import sse_chat_stream  # noqa: E402


def _payload():
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "firefly-gpt-image",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "![img](http://x/1.png)"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 1056,
            "total_tokens": 1066,
            "input_tokens": 10,
            "output_tokens": 1056,
            "output_tokens_details": {"image_tokens": 1056},
            "completion_tokens_details": {"image_tokens": 1056},
        },
    }


def _chunks(payload):
    events = list(sse_chat_stream(payload))
    assert events[-1] == "data: [DONE]\n\n"
    return [json.loads(e[len("data: "):]) for e in events[:-1]]


def test_stream_carries_usage():
    payload = _payload()
    chunks = _chunks(payload)
    # 至少一个 chunk 带完整 usage(sub2api 逐 chunk 解析,取最后一次)
    with_usage = [c for c in chunks if c.get("usage")]
    assert with_usage, "no chunk carries usage — streaming requests bill zero"
    assert with_usage[-1]["usage"] == payload["usage"]
    # 图像计费字段必须原样保留
    assert (
        with_usage[-1]["usage"]["output_tokens_details"]["image_tokens"] == 1056
    )


def test_stream_chunk_shape_unchanged():
    chunks = _chunks(_payload())
    # 首块: 角色+内容; 末块: finish_reason=stop; 所有块 choices 非空,
    # 不打破下游客户端对 choices[0] 的假设
    assert chunks[0]["choices"][0]["delta"]["content"] == "![img](http://x/1.png)"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    for c in chunks:
        assert c["choices"], "chunk with empty choices would break naive clients"
        assert c["object"] == "chat.completion.chunk"
        assert c["id"] == "chatcmpl-test123"


def test_stream_without_usage_field_is_safe():
    payload = _payload()
    payload.pop("usage")
    chunks = _chunks(payload)
    assert chunks[0]["choices"][0]["delta"]["content"]


if __name__ == "__main__":
    test_stream_carries_usage()
    test_stream_chunk_shape_unchanged()
    test_stream_without_usage_field_is_safe()
    print("OK")
