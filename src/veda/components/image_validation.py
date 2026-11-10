"""Is the file really the image type the client declared? Reads headers only, so it's cheap
and refuses oversized images before their pixels are decoded."""

import io

from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from veda.entity.config_entity import ImageConfig

register_heif_opener()  # lets Pillow open HEIC (iPhone) files

# Declared MIME type -> the format Pillow detects from the file's own bytes.
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP", "image/heic": "HEIF"}


class InvalidImage(Exception):
    """The file can't become a photo. Permanent: retrying won't help."""


class ImageValidation:
    def __init__(self, config: ImageConfig):
        self.config = config

    def validate(self, data: bytes, mime_type: str) -> Image.Image:
        """The opened (not yet decoded) image, or InvalidImage."""
        try:
            image = Image.open(io.BytesIO(data))
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as e:
            raise InvalidImage("Not a readable image") from e

        if image.format != FORMATS.get(mime_type):
            raise InvalidImage(f"File content is {image.format or 'unknown'}, not {mime_type}")
        width, height = image.size
        if width * height > self.config.max_pixels:
            raise InvalidImage(f"Image is {width}x{height}, above the pixel limit")
        return image
