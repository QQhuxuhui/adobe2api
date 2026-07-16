import json


def sse_chat_stream(payload: dict):
    """把非流式 chat.completion 响应转为 SSE chunk 序列。

    usage 挂在末个 finish chunk 上(choices 非空): 下游网关(sub2api/new-api)
    逐 chunk 解析 usage 用于按 token 计费,丢掉 usage 会导致流式生图计费为 0;
    同时不发 OpenAI include_usage 风格的空 choices 块,避免打破简单客户端
    对 chunk.choices[0] 的假设。
    """
    cid = payload["id"]
    created = payload["created"]
    model = payload["model"]
    content = payload["choices"][0]["message"]["content"]

    first = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }
    last = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    usage = payload.get("usage")
    if usage:
        last["usage"] = usage

    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
