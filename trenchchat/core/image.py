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
import warnings

from PIL import Image

MAX_IMAGE_DIMENSION = 1200  # px -- neither width nor height exceeds this
MAX_IMAGE_BYTES = 921600    # 900 KB  -- limit for compressed still images (below LXMF's 1 MB ceiling)
MAX_GIF_BYTES   = 921600    # 900 KB  -- limit for GIFs (below LXMF's 1 MB ceiling)
IMAGE_JPEG_QUALITY = 85

# Pillow's default (~178 Mpx) lets a small file expand into gigabytes of
# raster before the byte-size checks below, which measure compressed output.
MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# Each frame is retained as a full RGBA raster and copied again on rescale.
MAX_GIF_FRAMES = 300

# Scale factors tried in order when a GIF is too large.
# Each step reduces both dimensions by the given factor until one fits.
_GIF_SCALE_STEPS = (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)

# Ceiling on the total raster an inbound image may expand to across all
# frames. MAX_IMAGE_PIXELS bounds one frame; an animation multiplies it.
MAX_INBOUND_DECODED_PIXELS = MAX_IMAGE_PIXELS

# Formats an inbound image may declare, by magic bytes. Checked before Pillow
# is handed the payload at all, because Image.open() is only header-only for
# some formats: TIFF walks its whole IFD chain to count frames, and ICO fully
# decodes its largest frame inside open() -- both inside the very function
# meant to avoid decoding. prepare_image only ever emits JPEG or GIF, so
# nothing legitimate is outside this set.
_INBOUND_MAGIC = (
    b"\xff\xd8\xff",       # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
)


def _declared_format_is_allowed(image_bytes: bytes) -> bool:
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return True
    return any(image_bytes.startswith(magic) for magic in _INBOUND_MAGIC)


def inbound_image_is_sane(image_bytes: bytes) -> bool:
    """False if an inbound image's header declares an implausible decode.

    The byte cap bounds the payload, not what it expands to: a file well
    under it can declare enormous dimensions or thousands of frames, and
    those bytes are handed to the client's own image decoder. Only the
    header is read here -- no pixel data is decoded.

    The format is checked by magic bytes first: Image.open() is not
    header-only for every format Pillow supports, so handing it arbitrary
    bytes makes this function its own denial of service. Anything outside the
    formats a client actually sends is refused rather than parsed -- which
    also closes the fail-open case below for a format Pillow cannot read but a
    browser can.

    Bytes that get past the magic check and still do not parse are left alone.
    They cannot be shown to be hostile, they are stored as opaque blobs either
    way, and re-encoding every inbound image to normalise them would be lossy
    and costly on the low-power hardware Reticulum targets.
    """
    if not _declared_format_is_allowed(image_bytes):
        return False

    try:
        with warnings.catch_warnings():
            # Pillow only raises above twice MAX_IMAGE_PIXELS and merely warns
            # below that; both are rejections here.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as img:
                width, height = img.size
                frames = getattr(img, "n_frames", 1)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
    except Exception:
        return True

    if width <= 0 or height <= 0:
        return False
    if not isinstance(frames, int) or frames < 1:
        return False
    if frames > MAX_GIF_FRAMES:
        return False
    return width * height * frames <= MAX_INBOUND_DECODED_PIXELS


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
        while len(frames) < MAX_GIF_FRAMES:
            frames.append(img.convert("RGBA"))
            durations.append(img.info.get("duration", 100))
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    else:
        raise ValueError(
            f"GIF has more than {MAX_GIF_FRAMES} frames; refusing to decode"
        )
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
