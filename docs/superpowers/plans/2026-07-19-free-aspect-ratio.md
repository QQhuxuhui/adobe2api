# Free/Auto Aspect Ratio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `aspect_ratio=free` and `auto` across all image protocols while preserving the first input image's ratio when possible and mapping fixed-size models to their nearest supported ratio.

**Architecture:** Add a pure geometry result to the model resolver that separates payload ratio, usage ratio, output resolution, and an optional Adobe size override. Model catalog metadata supplies auto capability and ordered fixed-ratio candidates. Protocol routes load images before geometry resolution and pass the resulting size override through the shared image generation pipeline.

**Tech Stack:** Python 3, FastAPI, Pillow, pytest

## Global Constraints

- A fixed-ratio model ID wins only when the caller explicitly supplies it.
- `free` and `auto` are compatibility aliases.
- The first input image is the primary image; an unreadable first image returns 400.
- Adobe/Gemini auto requests use a first-image-derived, 16-pixel-aligned top-level size.
- GPT Image uses its own ordered fixed-ratio candidates and deterministic logarithmic distance.
- Video request validation and preprocessing remain unchanged.
- Image dimension inspection must not decode pixel data and must honor EXIF orientation.
- Auto size overrides stay within the selected tier's existing maximum edge.
- Auto size overrides carry a nearest fixed-ratio fallback candidate.
- Omitted models with explicit `free`/`auto` resolve to the dynamic default model ID.

---

### Task 1: Model Geometry Resolver

**Files:**
- Modify: `core/models/catalog.py`
- Modify: `core/models/payloads.py`
- Modify: `core/models/resolver.py`
- Modify: `core/models/__init__.py`
- Create: `tests/test_image_geometry.py`

**Interfaces:**
- Consumes: request dictionaries, optional public model ID, and loaded `(bytes, mime)` image tuples.
- Produces: `ResolvedImageGeometry(aspect_ratio, usage_ratio, output_resolution, model_id, output_size)` and `resolve_image_geometry(data, model_id, input_images)`.

- [ ] **Step 1: Write failing geometry tests**

```python
def test_auto_uses_primary_image_ratio_and_aligned_size():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "free", "quality": "2k"},
        "firefly-nano-banana-pro",
        [(png_bytes(1000, 1379), "image/png")],
    )
    assert resolved.aspect_ratio == "auto"
    assert resolved.usage_ratio == "1000:1379"
    assert resolved.output_size["width"] % 16 == 0
    assert resolved.output_size["height"] % 16 == 0

def test_gpt_maps_primary_image_to_nearest_supported_ratio():
    resolved = resolve_image_geometry(
        {"aspect_ratio": "auto"},
        "gpt-image-2",
        [(png_bytes(1000, 1379), "image/png")],
    )
    assert resolved.aspect_ratio == "3:4"
    assert resolved.usage_ratio == "3:4"
```

- [ ] **Step 2: Verify the tests fail for the missing resolver**

Run: `python -m pytest -q tests/test_image_geometry.py`

Expected: collection or assertion failure because `resolve_image_geometry` and capability metadata do not exist.

- [ ] **Step 3: Implement catalog capabilities and the resolver**

```python
@dataclass(frozen=True)
class ResolvedImageGeometry:
    aspect_ratio: str
    usage_ratio: str
    output_resolution: str
    model_id: str
    output_size: dict[str, int] | None
```

Implement `resolve_image_geometry(data: dict, model_id: Optional[str], input_images: Sequence[tuple[bytes, str]] = ()) -> ResolvedImageGeometry`. Implement `size_from_dimensions(width, height, output_resolution)` using the resolution tier's square pixel budget and rounding both dimensions to the nearest positive multiple of 16. Inspect only the first image and only for `free`/`auto` after explicit model-ratio precedence has been resolved.

- [ ] **Step 4: Run geometry tests**

Run: `python -m pytest -q tests/test_image_geometry.py tests/test_gemini_ratio_expansion.py`

Expected: PASS.

### Task 2: Adobe Payload Contract

**Files:**
- Modify: `core/models/payloads.py`
- Modify: `core/adobe_client.py`
- Modify: `core/image_generation.py`
- Create: `tests/test_image_payloads.py`

**Interfaces:**
- Consumes: `output_size: Mapping[str, int] | None` from resolved geometry.
- Produces: Adobe payload candidates where auto-with-image has the derived size and auto-without-image attempts a size-less payload before a 1:1 fallback.

- [ ] **Step 1: Write failing payload tests**

```python
def test_auto_payload_uses_size_override_without_aspect_ratio():
    payload = build_image_payload_candidates(
        prompt="draw", aspect_ratio="auto", output_resolution="2K",
        upstream_model_id="gemini-flash", upstream_model_version="nano-banana-2",
        output_size={"width": 1744, "height": 2400}, source_image_ids=["image-1"],
    )[0]
    assert payload["size"] == {"width": 1744, "height": 2400}
    assert "aspectRatio" not in payload["modelSpecificPayload"]

def test_auto_text_to_image_attempts_size_less_payload_then_square_fallback():
    candidates = build_image_payload_candidates(
        prompt="draw", aspect_ratio="auto", output_resolution="2K",
        upstream_model_id="gemini-flash", upstream_model_version="nano-banana-2",
    )
    assert "size" not in candidates[0]
    assert candidates[1]["size"] == {"width": 2048, "height": 2048}
```

- [ ] **Step 2: Verify payload tests fail**

Run: `python -m pytest -q tests/test_image_payloads.py`

Expected: FAIL because `output_size` is not accepted and auto currently produces a 16:9 size.

- [ ] **Step 3: Thread output size through the generation pipeline**

Add `output_size=None` to `build_image_payload_candidates`, `AdobeClient._build_payload_candidates`, `AdobeClient.generate`, and `generate_image_artifact`. Copy the override before placing it in a payload. For size-less auto requests return the auto candidate followed by an explicit 1:1 fallback candidate.

- [ ] **Step 4: Run payload and Adobe client tests**

Run: `python -m pytest -q tests/test_image_payloads.py tests/test_image_generation.py tests/test_adobe_deadline.py`

Expected: PASS.

### Task 3: OpenAI-Compatible Protocol Adapters

**Files:**
- Modify: `api/openai_responses.py`
- Modify: `api/routes/generation.py`
- Modify: `app.py`
- Modify: `tests/test_images_edits.py`
- Modify: `tests/test_openai_responses.py`
- Modify: `tests/test_generation_credit_context.py`

**Interfaces:**
- Consumes: `resolve_image_geometry` as an injected generation-router dependency.
- Produces: consistent `free`/`auto` behavior for generations, edits, Responses, and chat.

- [ ] **Step 1: Add failing endpoint regressions**

```python
def test_edits_free_uses_first_image_geometry(tmp_path: Path):
    response = client.post(
        "/v1/images/edits",
        data={"prompt": "edit", "model": "gpt-image-2", "aspect_ratio": "free"},
        files=[
            ("image[]", ("first.png", png_bytes(1000, 1379), "image/png")),
            ("image[]", ("second.png", png_bytes(1600, 900), "image/png")),
        ],
    )
    assert response.status_code == 200
    assert adobe.generate_kwargs["aspect_ratio"] == "3:4"

def test_responses_tool_aspect_ratio_overrides_top_level(tmp_path: Path):
    response = harness.http.post(
        "/v1/responses",
        json={
            "model": "gpt-image-2", "input": "draw", "aspect_ratio": "16:9",
            "tools": [{"type": "image_generation", "aspect_ratio": "free"}],
        },
    )
    assert response.status_code == 200
    assert harness.adobe.generate_kwargs["aspect_ratio"] == "1:1"
```

Also cover the Adobe output-size override, multiple-image first-item precedence, and invalid first-image 400 response in the same endpoint test modules.

- [ ] **Step 2: Verify endpoint tests fail**

Run: `python -m pytest -q tests/test_images_edits.py tests/test_openai_responses.py tests/test_generation_credit_context.py`

Expected: FAIL on dropped fields, pre-image ratio resolution, or absent geometry arguments.

- [ ] **Step 3: Adapt routes after image loading**

Add `aspect_ratio` to `ResponsesImageRequest`, `_TOOL_FIELDS`, and `_PARAM_FIELDS`. In all image handlers load images first, call `resolve_image_geometry`, pass `geometry.aspect_ratio` and `geometry.output_size` to generation, and use `geometry.usage_ratio` for usage. Keep video resolution on the existing resolver.

- [ ] **Step 4: Run OpenAI-compatible endpoint tests**

Run: `python -m pytest -q tests/test_images_edits.py tests/test_openai_responses.py tests/test_generation_credit_context.py tests/test_openai_responses_protocol.py`

Expected: PASS.

### Task 4: Gemini Native Adapter

**Files:**
- Modify: `api/routes/gemini_native.py`
- Modify: `tests/test_gemini_parser.py`
- Modify: `tests/test_gemini_native.py`

**Interfaces:**
- Consumes: parsed inline image bytes and the model spec's ordered ratios.
- Produces: Gemini image requests that accept `free`/`auto`, derive size from the first image, and leave video validation unchanged.

- [ ] **Step 1: Add failing Gemini tests**

Add parser tests accepting `free` and `auto` only for image families, plus route tests asserting `aspect_ratio="auto"`, a primary-image-derived `output_size`, and unchanged video rejection.

- [ ] **Step 2: Verify Gemini tests fail**

Run: `python -m pytest -q tests/test_gemini_parser.py tests/test_gemini_native.py`

Expected: FAIL with `Unsupported aspectRatio` or missing generation arguments.

- [ ] **Step 3: Resolve Gemini image geometry before generation**

Permit the two compatibility values in image parsing. Call the shared requested-ratio helper with `supports_auto=True`, `spec.aspect_ratios`, `parsed.images`, and `parsed.image_size`; pass its payload ratio and size override to `client.generate`. Map invalid primary images to Gemini `INVALID_ARGUMENT` responses.

- [ ] **Step 4: Run Gemini regressions**

Run: `python -m pytest -q tests/test_gemini_parser.py tests/test_gemini_native.py tests/test_gemini_ratio_expansion.py`

Expected: PASS.

### Task 5: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`

**Interfaces:**
- Consumes: final implemented behavior.
- Produces: user documentation for `free`/`auto` and verified regression status.

- [ ] **Step 1: Replace obsolete auto warnings**

Document that `free` and `auto` are aliases, the first input image controls geometry, fixed-ratio model IDs win, GPT maps to its nearest supported ratio, and Adobe auto requests preserve the first image ratio through the size override.

- [ ] **Step 2: Run formatting and focused tests**

Run: `git diff --check`

Run: `python -m pytest -q tests/test_image_geometry.py tests/test_image_payloads.py tests/test_images_edits.py tests/test_openai_responses.py tests/test_gemini_parser.py tests/test_gemini_native.py`

Expected: no whitespace errors and all tests PASS.

- [ ] **Step 3: Run the complete test suite**

Run: `python -m pytest -q`

Expected: PASS, with any environment-only skips reported.
