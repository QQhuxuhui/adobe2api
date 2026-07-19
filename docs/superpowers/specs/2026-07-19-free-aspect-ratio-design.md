# Free/Auto Aspect Ratio Compatibility Design

## Goal

Restore the historical `aspect_ratio=free` behavior across every supported
image-to-image protocol while respecting each upstream model's output-size
capabilities.

`free` and `auto` are equivalent compatibility values. They request an output
ratio based on the primary input image when one exists. Explicit ratios and
fixed-ratio model IDs keep their current behavior.

## Supported Entry Points

The same internal resolution flow applies to:

- `POST /v1/chat/completions`
- `POST /v1/images/edits`
- `POST /v1/responses`
- Gemini native `generateContent` image requests
- `POST /v1/images/generations` for requests without input images

Each protocol remains responsible for parsing its own request and loading its
input image bytes. Ratio selection happens only after those bytes are
available.

## Unified Resolution Rules

Ratio precedence is:

1. A fixed-ratio model ID always wins.
2. An explicit supported ratio such as `16:9` is used unchanged.
3. `free` or `auto` with input images uses the first image as the primary
   image.
4. `free` or `auto` without an input image uses `size` when present.
5. Without an input image or `size`, an auto-capable upstream receives `auto`;
   a fixed-size upstream falls back to `1:1`.

Invalid values other than `free` and `auto` keep the existing compatibility
fallback unless a protocol already requires strict validation.

## Model Capability Routing

The resolver returns both the upstream ratio and an accounting ratio.

For Adobe auto-capable image families, `free`/`auto` with an input image is
sent upstream as `auto` (or represented by omitting `aspectRatio`, according to
the existing payload builder). This lets Adobe derive the output from the
primary reference image. Without an input image or `size`, `auto` is still sent
upstream because the upstream schema accepts it, but the chosen output ratio is
left to Adobe and the accounting ratio uses the deterministic `1:1` estimate.

For `gpt-image:2` and other fixed-size families, the primary image dimensions
are read locally with Pillow. The source width-to-height ratio is mapped to the
closest ratio supported by that model, then the existing pixel-size table is
used. If no input image or `size` exists, the ratio is `1:1`.

With an input image, the accounting ratio records the primary image's reduced
width-to-height ratio for auto-capable models and the selected standard ratio
for fixed-size models. This keeps orientation-based usage calculation correct
without passing an unsupported value into the GPT Image payload builder.

## Multiple Images

One request produces one output image and therefore has one output ratio. The
first uploaded image is the primary image and determines `free`/`auto` ratio
selection. Remaining images are uploaded unchanged as content references;
they are not cropped, resized, or stretched by this service.

Generating one output per input image is explicitly out of scope because it
would change response shapes, request cost, and retry semantics. Callers that
need separate output ratios must submit separate requests.

## Components

A shared image-ratio helper will:

- normalize `free` and `auto`;
- inspect the first valid input image dimensions;
- choose `auto`, a nearest supported ratio, or a deterministic fallback based
  on model capability;
- expose a separate ratio for payload generation and usage accounting.

Protocol adapters will load images before invoking this helper. The existing
model resolver remains responsible for fixed model IDs, quality, resolution,
and `size` parsing.

The Responses request parser will accept `aspect_ratio` as a compatibility
field at both the top level and inside the image-generation tool. The images
edits route will stop dropping its multipart `aspect_ratio` field. Gemini will
accept `free`/`auto` only for image generation; video validation remains
unchanged.

## Error Handling

Unreadable primary image bytes return the protocol's existing invalid-image
400 response when ratio inspection is required. Empty or absent image lists do
not trigger image decoding.

An explicit fixed ratio never requires image decoding. Unsupported upstream
ratios are resolved before payload construction, so `gpt-image:2` will not
receive `auto` and will not fail with an avoidable 422-style error.

## Testing

Regression tests will cover:

- `free` and `auto` through chat, images edits, Responses, and Gemini;
- landscape, portrait, square, and non-standard primary image dimensions;
- `auto` forwarding for auto-capable model families;
- nearest-standard-ratio mapping for `gpt-image:2`;
- fixed-ratio model precedence;
- `size` fallback and the no-image/no-size model-specific fallback;
- multiple images with different dimensions, proving the first image controls
  output ratio while all images are still uploaded unchanged;
- unchanged behavior for explicit supported ratios and video requests.
