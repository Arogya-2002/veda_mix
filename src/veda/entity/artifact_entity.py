"""What components hand to each other and to the pipeline."""

from dataclasses import dataclass
from datetime import datetime

from veda.constants.enums import ImageFormat, PhotoVariantName


@dataclass(frozen=True)
class ImageVariant:
    name: PhotoVariantName
    data: bytes
    format: ImageFormat
    width: int
    height: int

    @property
    def content_type(self) -> str:
        return f"image/{self.format.lower()}"


@dataclass(frozen=True)
class ProcessedImage:
    width: int  # as displayed, EXIF rotation applied
    height: int
    taken_at: datetime | None  # camera's local clock; EXIF has no reliable time zone
    camera_make: str | None
    camera_model: str | None
    variants: tuple[ImageVariant, ...]
