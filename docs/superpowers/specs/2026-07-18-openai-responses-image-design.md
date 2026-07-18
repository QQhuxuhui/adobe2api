# Adobe2api OpenAI Responses Image Protocol Design

**Date:** 2026-07-18

**Status:** Approved direction, implementation pending

## Background

Adobe2api currently exposes image generation through:

- `POST /v1/images/generations`
- `POST /v1/images/edits`
- `POST /v1/chat/completions`, which returns a Markdown image URL

It does not expose `POST /v1/responses`. When sub2api routes an inbound
Responses request to the Adobe account, its capability probe marks that account
as Chat Completions-only. Sub2api therefore converts the request to
`/v1/chat/completions`, receives Markdown text, and converts that text back into
a Responses `message` output item.

The image is generated successfully, but clients that require the Responses
image tool protocol do not see an `image_generation_call`. sub2api also records
`image_count=0` because the final response contains text rather than an image
tool result.

Official OpenAI accounts work through sub2api because those accounts natively
return Responses image tool output. Adobe2api should provide the same protocol
boundary instead of requiring sub2api to reverse-engineer provider-specific
Markdown.

## Goals

1. Add a native `POST /v1/responses` endpoint for Adobe-backed GPT image models.
2. Return a final `image_generation_call` whose `result` is base64 image data.
3. Support both non-streaming JSON and streaming SSE Responses clients.
4. Accept the two request shapes encountered in the current gateway chain:
   - an image-only top-level model such as `gpt-image-2`;
   - a Responses-capable top-level model with an `image_generation` tool whose
     `model` selects the Adobe image model.
5. Reuse the existing Adobe generation, token rotation/retry, progress, request
   log, preview, usage, and credits tracking behavior.
6. Preserve the existing Images API and Chat Completions behavior.
7. Make the Adobe account safe to configure as native Responses plus automatic
   passthrough in sub2api.

## Non-goals

- General text inference through `/v1/responses`.
- Function, web search, computer use, or other non-image Responses tools.
- `/v1/responses/compact`.
- Responses WebSocket support.
- OpenAI Files API support or `file_id` image inputs.
- Mask editing through Responses. Multipart `/v1/images/edits` remains the
  supported compatibility endpoint for that workflow.
- Partial preview images. Adobe generation supplies progress metadata but does
  not supply intermediate image bytes.

## Considered Approaches

### 1. Native Responses adapter in Adobe2api (selected)

Adobe2api parses the Responses request, calls its existing image generation
path, and serializes the result as Responses JSON or SSE.

Advantages:

- The adapter still has the original request parameters and final image bytes.
- No HTTP re-download is required to create the base64 result.
- Adobe errors, token retries, progress, credits, and logs retain their native
  semantics.
- sub2api can use normal passthrough behavior, as it does for official accounts.

### 2. Convert Markdown responses inside sub2api

sub2api could detect `![Generated Image](...)`, download the URL, and construct
an `image_generation_call`.

This is rejected because Markdown is not a provider contract, image parameters
and metadata have already been lost, URL download introduces another failure
boundary, and generic gateway code becomes coupled to Adobe2api output.

### 3. Rewrite the endpoint in new-api

new-api could route `/v1/responses` to `/v1/images/generations` and translate the
response.

This is rejected because routing would need provider-specific body and response
knowledge, while direct sub2api and Adobe2api clients would remain incompatible.

## Architecture

The implementation has four focused units:

1. `api/openai_responses.py`
   - Pure parsing and serialization helpers.
   - No Adobe client, token pool, filesystem, or FastAPI global dependencies.
   - Converts supported Responses input into a normalized image request.
   - Builds final Responses JSON and SSE events.

2. `core/image_generation.py`
   - Provides a small `GeneratedImageArtifact` result type and one shared image
     execution function.
   - Owns the existing Adobe `client.generate` call, generated PNG write,
     generated-file accounting callback, and final artifact bytes.
   - Is adopted by Images generations, Images edits, image Chat Completions, and
     Responses so those routes cannot drift in their Adobe execution behavior.

3. `api/routes/generation.py`
   - Registers `POST /v1/responses` in the existing generation router so it can
     reuse the injected authentication, Adobe client, retry, progress, preview,
     error, and logging callbacks.
   - Performs the Adobe generation operation and image format conversion.
   - Leaves existing routes behaviorally unchanged.

4. `app.py`
   - Maps `/v1/responses` to the request-log operation `responses.create`.
   - Lets the existing middleware extract model and prompt metadata from JSON.

The parser/serializer boundary keeps protocol rules independently testable.
The shared image executor performs Adobe and filesystem I/O, while each route
remains responsible for its own request parsing and response protocol.

## Request Contract

### Endpoint and authentication

`POST /v1/responses` uses the same bearer-token authentication as the existing
OpenAI-compatible routes. The request body must be a JSON object.

### Image model selection

The selected image model is resolved in this order:

1. The `model` field on the first `tools[]` item whose `type` is
   `image_generation`, when present and non-empty.
2. The top-level `model` when it names an Adobe2api image model.
3. `gpt-image-2` when an `image_generation` tool is present without its own
   model. This covers the official Responses request shape where the top-level
   model is a text model and the image tool selects its own backend implicitly.

The selected image model must resolve through the existing model catalog. A
top-level text model is accepted only when an `image_generation` tool selects a
valid Adobe image model. A request with neither a valid image-only top-level
model nor an image tool returns HTTP 400.

The response echoes the inbound top-level model. Credits and Adobe generation
use the selected image model.

### Prompt extraction

The prompt is resolved from:

1. a non-empty string in `input`;
2. the last user item in an `input` array, concatenating its `input_text` or
   `text` content parts;
3. the compatibility field `prompt`.

An empty prompt returns HTTP 400 with OpenAI error type
`invalid_request_error`.

### Input images

The last user input item may contain up to six `input_image` parts. This first
version accepts an `image_url` containing an `http://`, `https://`, or `data:`
URL. These parts are normalized into the existing Chat Completions image input
shape before calling the existing image loader.

`file_id` and `input_image_mask` return HTTP 400 with a clear unsupported-field
message. Supporting them would require a Files API and is outside this design.

### Tool selection

Because this endpoint is image-only, these values allow generation:

- omitted `tool_choice`;
- `auto`;
- `required`;
- an object selecting `image_generation`.

`none` or a choice that explicitly selects a different tool returns HTTP 400.
The endpoint never silently returns text instead of an image.

### Image parameters

Image-tool fields take precedence over equivalent top-level compatibility
fields. Supported fields are:

| Field | Behavior |
|---|---|
| `size` | Passed to existing ratio and resolution logic. |
| `quality` | Passed to existing 1K/2K/4K quality mapping. |
| `output_format` | Supports `png`, `jpeg`, `jpg`, and `webp`; defaults to `png`. |
| `output_compression` | Integer 0-100 for JPEG/WebP encoding; ignored for PNG. |
| `background` | Accepts `auto` and `opaque`; `transparent` returns HTTP 400. |
| `moderation` | Accepted for compatibility; Adobe upstream moderation remains authoritative. |
| `action` | Accepts `auto`, `generate`, and `edit`; `edit` requires at least one input image. |
| `partial_images` | Accepted only as `0`; positive values return HTTP 400. |

Unknown top-level Responses fields do not affect Adobe generation. Unknown
fields inside `image_generation` return HTTP 400 so clients are not told that
an unsupported image option was honored. `input_fidelity` and
`input_image_mask` also return HTTP 400 because Adobe2api cannot preserve their
documented semantics.

### Output format conversion

Adobe's generated PNG remains the stored preview artifact. When the client asks
for JPEG or WebP, Pillow converts the in-memory final result before base64
encoding. The stored preview remains PNG so existing generated-file serving and
admin preview behavior do not change.

## Non-streaming Response

HTTP 200 returns a Responses object with one completed image tool output:

```json
{
  "id": "resp_<id>",
  "object": "response",
  "created_at": 1784332800,
  "status": "completed",
  "model": "gpt-image-2",
  "output": [
    {
      "id": "ig_<id>",
      "type": "image_generation_call",
      "status": "completed",
      "result": "<base64 image>"
    }
  ],
  "usage": {
    "input_tokens": 25,
    "output_tokens": 272,
    "total_tokens": 297,
    "input_tokens_details": {
      "text_tokens": 25,
      "image_tokens": 0
    },
    "output_tokens_details": {
      "image_tokens": 272
    }
  }
}
```

The response does not expose the generated public URL as protocol output. The
URL remains available in request-log preview metadata.

## Streaming Response

When `stream=true`, the endpoint performs the same generation and then emits a
valid SSE sequence:

1. `response.created` with an in-progress response and empty output.
2. `response.output_item.added` with an in-progress
   `image_generation_call` item.
3. `response.output_item.done` with the completed item and final base64 result.
4. `response.completed` with the complete response and usage.
5. `data: [DONE]`.

The endpoint does not emit `response.image_generation_call.partial_image`
because Adobe does not provide intermediate image bytes. The HTTP request stays
open while generation runs, matching current Chat Completions streaming
behavior.

## Errors

Validation failures return the standard shape:

```json
{
  "error": {
    "message": "<message>",
    "type": "invalid_request_error"
  }
}
```

Existing domain errors keep their current mappings:

- quota exhausted: HTTP 429, `rate_limit_error`;
- invalid Adobe token: HTTP 401, `authentication_error`;
- temporary Adobe failure or exhausted retry pool: HTTP 503, `server_error`;
- unexpected error: HTTP 500, `server_error`, with an internal error code.

Errors occurring before the first SSE event return normal JSON with a non-2xx
status. Generation completes before streaming begins, so the endpoint never
needs to encode a late failure as an SSE terminal event.

## Logging, Credits, and Billing Metadata

- `app.py` maps the endpoint to `responses.create`.
- Request logs store the selected image model and prompt preview, not the full
  request or base64 output.
- The endpoint calls `set_request_credit_context` with the selected image model
  and resolved output resolution.
- The endpoint uses `run_with_token_retries` with operation name
  `responses.create`.
- The final usage reuses `build_image_usage` and exposes Responses token field
  names, including image output tokens.
- `set_request_preview` records the generated URL for the admin log page.
- The base64 result must never be copied into request logs, error details, or
  live-request state.

## sub2api Configuration After Deployment

The Adobe account must not be changed until the new endpoint passes direct
tests. After deployment:

1. Set **Responses API support** to **Force Responses**.
2. Enable **Auto passthrough (auth only)**.
3. Keep **Codex image tool** at **Follow channel**.
4. Keep **WS mode** off.
5. Keep **2K/4K image support** enabled.
6. Set **Compact mode** to **Force Off** and leave compact model mapping empty.
7. Disable the unsupported **Embeddings** capability.

Automatic passthrough is important after native support is enabled: it prevents
sub2api from rewriting an image-only top-level model into a text-model request
and preserves Adobe2api's Responses output exactly.

## Test Strategy

### Pure protocol tests

- Parse a string `input` with a top-level `gpt-image-2` model.
- Parse the array form with `input_text` and `input_image` parts.
- Select the image model and parameters from an `image_generation` tool.
- Verify tool parameters override top-level compatibility fields.
- Reject missing prompts, unsupported models, `tool_choice=none`, transparent
  backgrounds, positive partial image counts, unknown image tool fields, and
  `file_id` inputs.
- Verify PNG, JPEG, and WebP result encoding.
- Verify non-streaming response and streaming event schemas.

### Route tests

- Require service authentication.
- Generate one image through the injected fake Adobe client.
- Verify the final output item is `image_generation_call`, never `message`.
- Verify `result` decodes to the generated image bytes.
- Verify streaming includes the required event order and final usage.
- Verify input images are uploaded and passed as source image IDs.
- Verify resolved model/resolution credit context, preview URL, token retry
  operation name, and error mappings.

### Regression tests

- Existing `/v1/images/generations`, `/v1/images/edits`, and
  `/v1/chat/completions` tests remain green.
- Request logger tests verify `responses.create` and confirm base64 output is not
  persisted.
- The complete Python test suite runs before deployment.

### Deployment verification

1. Test Adobe2api directly with non-streaming and streaming requests.
2. Confirm both responses contain `image_generation_call` and valid base64.
3. Change the sub2api Adobe account settings listed above.
4. Repeat the same requests through `https://tomapi.top/`.
5. Confirm sub2api usage logs record `image_count=1`.
6. Only then point new-api channel 185 at the sub2api Adobe token.

## Rollback

The endpoint is additive. If compatibility problems occur:

1. Disable sub2api automatic passthrough.
2. Return Responses API support to Auto/Chat Completions.
3. Existing Images API and Chat Completions clients continue working without an
   Adobe2api rollback.
