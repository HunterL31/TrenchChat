"""
Image compression utility for TrenchChat message attachments.

Images attached to messages are resized and JPEG-compressed before being
embedded inline in the LXMF message fields.  LXMF automatically promotes
large messages to RNS Resource transfer, so images within the size limit
are delivered transparently over any transport.
"""

import io

from PIL import Image

MAX_IMAGE_DIMENSION = 800   # px -- neither width nor height exceeds this
MAX_IMAGE_BYTES = 327680    # 320 KB
IMAGE_JPEG_QUALITY = 85


def compress_image(image_bytes: bytes) -> bytes:
    """Resize and JPEG-compress raw image bytes for inline message attachment.

    Preserves aspect ratio so neither dimension exceeds MAX_IMAGE_DIMENSION.
    Raises ValueError if the compressed result exceeds MAX_IMAGE_BYTES (which
    should not occur for typical photos at this resolution and quality, but
    acts as a hard safety check).
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
