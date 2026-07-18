"""Pure request and response helpers for the OpenAI Responses image protocol."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from PIL import Image


class ResponsesRequestError(ValueError):
    """Raised when a Responses image request cannot be supported."""

    def __init__(self, message: str, param: str | None = None):
        super().__init__(message)
        self.param = param


@dataclass(frozen=True)
class ResponsesImageRequest:
    """Normalized subset of a Responses request used by Adobe image routes."""

    inbound_model: str
    image_model: str
    prompt: str
    input_image_urls: tuple[str, ...]
    stream: bool
    size: str | None
    quality: str | None
    output_format: str
    output_compression: int | None
    background: str
    moderation: str | None
    action: str
    partial_images: int

    def image_loader_messages(self) -> list[dict[str, Any]]:
        """Return input images in the shape accepted by the existing loader."""

        return [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}}
                    for url in self.input_image_urls
                ],
            }
        ]


_TOOL_FIELDS = {
    "type",
    "model",
    "size",
    "quality",
    "output_format",
    "output_compression",
    "background",
    "moderation",
    "action",
    "partial_images",
    "input_fidelity",
    "input_image_mask",
}
_PARAM_FIELDS = _TOOL_FIELDS - {"type", "model"}


def _last_user_content(input_value: Any) -> tuple[str, tuple[str, ...]]:
    if isinstance(input_value, str):
        return input_value.strip(), ()
    if not isinstance(input_value, list):
        return "", ()
    for item in reversed(input_value):
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content.strip(), ()
        texts: list[str] = []
        urls: list[str] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                part_type = str(part.get("type") or "")
                if part_type in {"input_text", "text"}:
                    text = str(part.get("text") or "").strip()
                    if text:
                        texts.append(text)
                elif part_type in {"input_image", "image_url"}:
                    if part.get("file_id"):
                        raise ResponsesRequestError(
                            "file_id input images are not supported", "input"
                        )
                    image_url = part.get("image_url")
                    if isinstance(image_url, Mapping):
                        image_url = image_url.get("url")
                    url = str(image_url or "").strip()
                    if not url:
                        raise ResponsesRequestError(
                            "input_image.image_url is required", "input"
                        )
                    urls.append(url)
        if len(urls) > 6:
            raise ResponsesRequestError("at most 6 input images are supported", "input")
        return "\n".join(texts).strip(), tuple(urls)
    return "", ()


def _image_tool(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    tools = data.get("tools")
    if tools is None:
        return None
    if not isinstance(tools, list):
        raise ResponsesRequestError("tools must be an array", "tools")
    for tool in tools:
        if isinstance(tool, Mapping) and str(tool.get("type") or "") == "image_generation":
            unknown = set(tool) - _TOOL_FIELDS
            if unknown:
                field = sorted(unknown)[0]
                raise ResponsesRequestError(
                    f"unsupported image_generation field: {field}", f"tools.{field}"
                )
            return tool
    return None


def _validate_tool_choice(choice: Any) -> None:
    if choice is None:
        return
    if isinstance(choice, str):
        if choice in {"auto", "required"}:
            return
        if choice == "none":
            raise ResponsesRequestError(
                "tool_choice must allow image_generation", "tool_choice"
            )
        raise ResponsesRequestError(
            "tool_choice must select image_generation", "tool_choice"
        )
    if isinstance(choice, Mapping) and str(choice.get("type") or "") == "image_generation":
        return
    raise ResponsesRequestError("tool_choice must select image_generation", "tool_choice")


def parse_responses_image_request(
    data: Mapping[str, Any], image_model_ids: Iterable[str]
) -> ResponsesImageRequest:
    """Normalize supported Responses image request forms.

    The helper intentionally knows nothing about the model catalog or Adobe
    client.  Callers provide the set of image model IDs that are available.
    """

    if not isinstance(data, Mapping):
        raise ResponsesRequestError("request body must be an object")
    inbound_model = str(data.get("model") or "").strip()
    if not inbound_model:
        raise ResponsesRequestError("model is required", "model")
    models = set(image_model_ids)
    tool = _image_tool(data)
    if tool and "input_image_mask" in tool:
        raise ResponsesRequestError(
            "input_image_mask is not supported", "tools.input_image_mask"
        )
    if tool and "input_fidelity" in tool:
        raise ResponsesRequestError(
            "input_fidelity is not supported", "tools.input_fidelity"
        )
    image_model = str((tool or {}).get("model") or "").strip()
    if not image_model and inbound_model in models:
        image_model = inbound_model
    if not image_model and tool is not None:
        image_model = "gpt-image-2"
    if not image_model:
        raise ResponsesRequestError(
            "image_generation tool is required for this model", "tools"
        )
    if image_model not in models:
        raise ResponsesRequestError(f"unsupported image model: {image_model}", "model")
    _validate_tool_choice(data.get("tool_choice"))
    prompt, input_urls = _last_user_content(data.get("input"))
    if not prompt:
        prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise ResponsesRequestError("input is required", "input")
    effective = {key: data.get(key) for key in _PARAM_FIELDS if key in data}
    if tool:
        effective.update({key: tool[key] for key in _PARAM_FIELDS if key in tool})
    output_format = str(effective.get("output_format") or "png").lower().strip()
    if output_format == "jpg":
        output_format = "jpeg"
    if output_format not in {"png", "jpeg", "webp"}:
        raise ResponsesRequestError(
            "output_format must be png, jpeg, or webp", "output_format"
        )
    compression = effective.get("output_compression")
    if compression is not None:
        if (
            isinstance(compression, bool)
            or not isinstance(compression, int)
            or not 0 <= compression <= 100
        ):
            raise ResponsesRequestError(
                "output_compression must be an integer from 0 to 100",
                "output_compression",
            )
    background = str(effective.get("background") or "auto").lower().strip()
    if background == "transparent":
        raise ResponsesRequestError(
            "transparent backgrounds are not supported", "background"
        )
    if background not in {"auto", "opaque"}:
        raise ResponsesRequestError("background must be auto or opaque", "background")
    partial_images = effective.get("partial_images", 0)
    if (
        isinstance(partial_images, bool)
        or not isinstance(partial_images, int)
        or partial_images != 0
    ):
        raise ResponsesRequestError("partial_images must be 0", "partial_images")
    action = str(effective.get("action") or "auto").lower().strip()
    if action not in {"auto", "generate", "edit"}:
        raise ResponsesRequestError(
            "action must be auto, generate, or edit", "action"
        )
    if action == "edit" and not input_urls:
        raise ResponsesRequestError("action=edit requires an input image", "action")
    return ResponsesImageRequest(
        inbound_model=inbound_model,
        image_model=image_model,
        prompt=prompt,
        input_image_urls=input_urls,
        stream=bool(data.get("stream", False)),
        size=str(effective["size"]).strip()
        if effective.get("size") is not None
        else None,
        quality=str(effective["quality"]).strip()
        if effective.get("quality") is not None
        else None,
        output_format=output_format,
        output_compression=compression,
        background=background,
        moderation=str(effective["moderation"]).strip()
        if effective.get("moderation") is not None
        else None,
        action=action,
        partial_images=partial_images,
    )


def encode_image_result(
    image_bytes: bytes, output_format: str, output_compression: int | None
) -> str:
    """Encode final image bytes as base64, converting format when requested."""

    if output_format == "png":
        encoded = image_bytes
    else:
        with Image.open(io.BytesIO(image_bytes)) as source:
            output = io.BytesIO()
            if output_format == "jpeg":
                source.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=90
                    if output_compression is None
                    else output_compression,
                )
            else:
                source.save(
                    output,
                    format="WEBP",
                    quality=90
                    if output_compression is None
                    else output_compression,
                )
            encoded = output.getvalue()
    return base64.b64encode(encoded).decode("ascii")


def _responses_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        ),
    }
    result["total_tokens"] = int(
        usage.get("total_tokens") or result["input_tokens"] + result["output_tokens"]
    )
    for key in ("input_tokens_details", "output_tokens_details"):
        if isinstance(usage.get(key), Mapping):
            result[key] = dict(usage[key])
    return result


def build_responses_image_response(
    *,
    response_id: str,
    item_id: str,
    created_at: int,
    model: str,
    result_b64: str,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the final non-streaming Responses image result."""

    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": item_id,
                "type": "image_generation_call",
                "status": "completed",
                "result": result_b64,
            }
        ],
        "usage": _responses_usage(usage),
    }


def _sse(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def iter_responses_image_sse(response: Mapping[str, Any]):
    """Yield the standard final-only Responses image event sequence."""

    final_response = dict(response)
    final_item = dict(final_response["output"][0])
    pending_response = dict(final_response)
    pending_response["status"] = "in_progress"
    pending_response["output"] = []
    pending_response.pop("usage", None)
    pending_item = {key: value for key, value in final_item.items() if key != "result"}
    pending_item["status"] = "in_progress"
    events = [
        (
            "response.created",
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": pending_response,
            },
        ),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "sequence_number": 1,
                "output_index": 0,
                "item": pending_item,
            },
        ),
        (
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "sequence_number": 2,
                "output_index": 0,
                "item": final_item,
            },
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "sequence_number": 3,
                "response": final_response,
            },
        ),
    ]
    for event_name, payload in events:
        yield _sse(event_name, payload)
    yield "data: [DONE]\n\n"
