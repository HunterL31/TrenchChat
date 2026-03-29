"""
Image compression utility for TrenchChat message attachments.

Images attached to messages are resized and JPEG-compressed before being
embedded inline in the LXMF message fields.  LXMF automatically promotes
large messages to RNS Resource transfer, so images within the size limit
are delivered transparently over any transport.

Both still images and GIFs share the same 900 KB ceiling (MAX_IMAGE_BYTES /
MAX_GIF_BYTES), which leaves headroom below the LXMF default 1 MB delivery
limit for protocol framing overhead.  GIFs are re-scaled by dimension if they
exceed the limit so animation frames are preserved.
"""

import io

from PIL import Image

MAX_IMAGE_DIMENSION = 1200  # px -- neither width nor height exceeds this
MAX_IMAGE_BYTES = 921600    # 900 KB  -- limit for compressed still images (below LXMF's 1 MB ceiling)
MAX_GIF_BYTES   = 921600    # 900 KB  -- limit for GIFs (below LXMF's 1 MB ceiling)
IMAGE_JPEG_QUALITY = 85

# Scale factors tried in order when a GIF is too large.
# Each step reduces both dimensions by the given factor until one fits.
_GIF_SCALE_STEPS = (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)


def compress_image(image_bytes: bytes) -> bytes:
    """Resize and JPEG-compress raw image bytes for inline message attachment.

    Preserves aspect ratio so neither dimension exceeds MAX_IMAGE_DIMENSION.
    Raises ValueError if the compressed result exceeds MAX_IMAGE_BYTES.
    At 1200 px and quality 85 this should not occur for typical photos, but
    acts as a hard safety check.

    GIFs should be handled via prepare_image() instead, which preserves
    animation by skipping JPEG conversion.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

    w, h = img.size
    if w > MAX_IMAGE_DIMENSION or h > MAX_IMAGE_DIMENSION:
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    result = buf.getvalue()

    if len(result) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Compressed image is {len(result)} bytes, exceeds {MAX_IMAGE_BYTES} limit"
        )
    return result


def _extract_gif_frames(image_bytes: bytes) -> tuple[list[Image.Image], list[int]]:
    """Extract all frames and their durations from a GIF.

    Each frame is converted to RGBA for consistent handling across palette modes.
    Returns (frames, durations) where both lists have the same length.
    """
    img = Image.open(io.BytesIO(image_bytes))
    frames: list[Image.Image] = []
    durations: list[int] = []
    try:
        while True:
            frames.append(img.convert("RGBA"))
            durations.append(img.info.get("duration", 100))
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    return frames, durations


def _encode_gif(frames: list[Image.Image], durations: list[int]) -> bytes:
    """Encode a sequence of RGBA frames into GIF bytes."""
    palette_frames = [f.convert("P", palette=Image.ADAPTIVE, colors=256) for f in frames]
    buf = io.BytesIO()
    palette_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=palette_frames[1:],
        loop=0,
        duration=durations,
        optimize=True,
    )
    return buf.getvalue()


def compress_gif(image_bytes: bytes) -> bytes:
    """Compress a GIF to fit within MAX_GIF_BYTES by scaling its dimensions.

    If the raw GIF already fits it is returned unchanged (fast path).
    Otherwise the GIF is re-encoded at progressively smaller sizes using the
    scale factors in _GIF_SCALE_STEPS.  All frames are preserved; only the
    pixel dimensions change.

    Raises ValueError if even the smallest scale step produces a GIF that
    still exceeds MAX_GIF_BYTES.
    """
    if len(image_bytes) <= MAX_GIF_BYTES:
        return image_bytes

    frames, durations = _extract_gif_frames(image_bytes)
    if not frames:
        raise ValueError("GIF contains no readable frames")

    original_w, original_h = frames[0].size

    for scale in _GIF_SCALE_STEPS:
        new_w = max(1, int(original_w * scale))
        new_h = max(1, int(original_h * scale))
        scaled = [f.resize((new_w, new_h), Image.LANCZOS) for f in frames]
        result = _encode_gif(scaled, durations)
        if len(result) <= MAX_GIF_BYTES:
            return result

    raise ValueError(
        f"GIF could not be compressed to fit within {MAX_GIF_BYTES} bytes "
        f"(original: {len(image_bytes)} bytes)"
    )


def is_gif(image_bytes: bytes) -> bool:
    """Return True if the raw bytes represent a GIF image."""
    return image_bytes[:6] in (b"GIF87a", b"GIF89a")


def prepare_image(image_bytes: bytes) -> tuple[bytes, bool]:
    """Prepare image bytes for transmission.

    Returns (data, gif) where gif is True when the original file is a GIF.

    GIFs are re-encoded at reduced dimensions if needed to fit within
    MAX_GIF_BYTES, preserving all animation frames.

    All other formats are JPEG-compressed via compress_image().
    """
    if is_gif(image_bytes):
        return compress_gif(image_bytes), True

    return compress_image(image_bytes), False
