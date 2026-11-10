"""Settings each component needs, built from the YAML files by veda.config.configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ImageConfig:
    thumb_px: int
    web_px: int
    thumb_quality: int
    web_quality: int
    max_pixels: int
