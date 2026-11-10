"""Original -> gallery copies (web + thumbnail, WebP).

Re-encoding drops every bit of metadata (EXIF, GPS, XMP) except the colour profile, so
guests never get the photographer's location. Camera rotation is applied to the pixels.
"""

import io
import math
from datetime import datetime

from PIL import ExifTags, Image, ImageOps

from veda.components.image_validation import InvalidImage
from veda.constants.enums import ImageFormat, PhotoVariantName
from veda.entity.artifact_entity import ImageVariant, ProcessedImage
from veda.entity.config_entity import ImageConfig

_ROTATED = {5, 6, 7, 8}  # EXIF orientations that swap width and height


class ImageProcessing:
    def __init__(self, config: ImageConfig):
        self.config = config

    def process(self, image: Image.Image) -> ProcessedImage:
        exif = image.getexif()
        width, height = image.size
        if exif.get(ExifTags.Base.Orientation) in _ROTATED:
            width, height = height, width
        icc_profile = image.info.get("icc_profile")

        try:
            _draft(image, self.config.web_px)
            image = _to_rgb(ImageOps.exif_transpose(image))  # decodes the pixels
        except (OSError, ValueError, SyntaxError) as e:  # truncated or corrupt data
            raise InvalidImage("Image data is damaged") from e

        web = _fit(image, self.config.web_px)
        thumb = _fit(web, self.config.thumb_px)  # from the web copy: much faster
        return ProcessedImage(
            width=width,
            height=height,
            taken_at=_taken_at(exif),
            camera_make=_text(exif.get(ExifTags.Base.Make)),
            camera_model=_text(exif.get(ExifTags.Base.Model)),
            variants=(
                _encode(PhotoVariantName.WEB, web, self.config.web_quality, icc_profile),
                _encode(PhotoVariantName.THUMB, thumb, self.config.thumb_quality, icc_profile),
            ),
        )


def _draft(image: Image.Image, long_edge: int) -> None:
    """JPEG only: decode at 1/2, 1/4 or 1/8 scale while still >= long_edge. A 24 MP photo
    then decodes in a fraction of the time and memory. Other formats ignore this."""
    w, h = image.size
    scale = long_edge / max(w, h)
    if scale < 1:
        image.draft(None, (math.ceil(w * scale), math.ceil(h * scale)))


def _to_rgb(image: Image.Image) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    mode = "RGBA" if has_alpha else "RGB"
    return image if image.mode == mode else image.convert(mode)


def _fit(image: Image.Image, long_edge: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)  # never enlarges
    return copy


def _encode(
    name: PhotoVariantName, image: Image.Image, quality: int, icc_profile: bytes | None
) -> ImageVariant:
    buf = io.BytesIO()
    # exif/xmp passed empty on purpose: Pillow would otherwise copy them from the original.
    image.save(
        buf, "WEBP", quality=quality, method=4, icc_profile=icc_profile or b"", exif=b"", xmp=b""
    )
    return ImageVariant(
        name=name,
        data=buf.getvalue(),
        format=ImageFormat.WEBP,
        width=image.width,
        height=image.height,
    )


def _text(value: object) -> str | None:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    text = str(value).strip("\x00 ") if value is not None else ""
    return text[:64] or None


def _taken_at(exif: Image.Exif) -> datetime | None:
    try:
        raw = exif.get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.DateTimeOriginal)
        return datetime.strptime(_text(raw) or "", "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError, KeyError):  # missing, "0000:00:00 00:00:00", or junk
        return None
