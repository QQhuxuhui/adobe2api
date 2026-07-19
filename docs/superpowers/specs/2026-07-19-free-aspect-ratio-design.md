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

1. A fixed-ratio model ID explicitly supplied by the caller always wins. The
   catalog default does not count as an explicit fixed-ratio selection.
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

Each image model declares whether it supports automatic aspect ratios and an
ordered list of its supported fixed ratios in the model catalog. Routing uses
these declarations rather than model-name string comparisons.

For Adobe auto-capable image families, `free`/`auto` with an input image is
represented by omitting `aspectRatio`. The service reads the first image locally
and supplies a top-level `size` scaled to the requested resolution tier with
dimensions aligned to 16 pixels. That size preserves the primary image ratio as
closely as the integer alignment permits, so additional reference images cannot
change which image controls the output geometry. The unaligned reduced source
ratio is retained for accounting.

Derived sizes are bounded by the largest existing standard size for the selected
resolution tier. The exact-ratio payload is followed by a model-specific nearest
fixed-ratio candidate, so an upstream rejection degrades to the closest supported
standard ratio instead of failing the request.

Without an input image or `size`, the primary Adobe payload omits both
`aspectRatio` and top-level `size`, which represents `auto` without accidentally
falling through `size_from_ratio()` to `16:9`. If the private Adobe 3P endpoint
rejects that form, the payload candidate fallback uses `1:1`; it never silently
substitutes `16:9` before the auto attempt.

For the public `gpt-image-2` model (Adobe upstream `modelId=gpt-image`,
`modelVersion=2`) and other fixed-size families, the primary image dimensions
are read locally with Pillow. The source width-to-height ratio is mapped to the
closest ratio supported by that model, then the existing pixel-size table is
used. If no input image or `size` exists, the ratio is `1:1`.

Nearest-ratio selection minimizes
`abs(log(source_width/source_height) - log(candidate_width/candidate_height))`.
Candidates come from the model's ordered fixed-ratio list; equal distances use
the first candidate in that list. This makes portrait and landscape comparisons
symmetric and tie handling deterministic.

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
- inspect the first input image dimensions and return a protocol-level 400 if
  that image cannot be decoded;
- choose `auto`, a nearest supported ratio, or a deterministic fallback based
  on model capability;
- expose separate values for payload ratio, top-level size override, and usage
  accounting ratio.

Protocol adapters will load images before invoking this helper. The existing
model resolver remains responsible for fixed model IDs, quality, resolution,
and `size` parsing.

The Responses request parser will accept `aspect_ratio` as a compatibility
field at both the top level and inside the image-generation tool. The images
edits route will stop dropping its multipart `aspect_ratio` field. Gemini will
accept `free`/`auto` only for image generation; video validation remains
unchanged.

## Error Handling

Unreadable first-image bytes return the protocol's existing invalid-image 400
response when ratio inspection is required. The resolver does not skip a broken
first image and promote the second image. Empty or absent image lists do not
trigger image decoding.

Dimension inspection reads metadata without decoding pixel data, applies EXIF
orientation before selecting a ratio, and maps Pillow decompression-bomb errors
to the same invalid-image 400 response.

When `free` or `auto` is explicit and the caller omits `model`, resolution uses
the dynamic `firefly-nano-banana-pro` model ID. Responses, logs, and credit
measurement therefore do not claim a fixed `16:9` model for an auto payload.

An explicit fixed ratio never requires image decoding. Unsupported upstream
ratios are resolved before payload construction, so `gpt-image-2` will not
receive `auto` and will not fail with an avoidable 422-style error.

## Testing

Regression tests will cover:

- `free` and `auto` through chat, images edits, Responses, and Gemini;
- landscape, portrait, square, and non-standard primary image dimensions;
- `auto` forwarding for auto-capable model families;
- auto-capable payloads use a primary-image-derived top-level `size` instead of
  the old implicit `16:9` size;
- no-image/no-size Adobe payloads attempt auto without a top-level `size` and
  expose a `1:1` fallback candidate;
- nearest-standard-ratio mapping for `gpt-image-2`;
- deterministic nearest-ratio tie handling using each model's own candidates;
- fixed-ratio model precedence;
- omitted model IDs do not inherit the default catalog model's fixed `16:9`
  suffix when `free`/`auto` is explicitly requested;
- `size` fallback and the no-image/no-size model-specific fallback;
- multiple images with different dimensions, proving the first image controls
  output ratio while all images are still uploaded unchanged;
- unreadable first images return 400 instead of falling through to another
  image;
- unchanged behavior for explicit supported ratios and video requests.
