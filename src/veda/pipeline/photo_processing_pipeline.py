"""validate -> process -> store, for one uploaded photo.

Runs in a Celery worker (veda.process_photo); also callable from main.py or a notebook.
S3 and database errors propagate so the task can retry; a bad image is final.
"""

from veda.components.image_processing import ImageProcessing
from veda.components.image_validation import ImageValidation, InvalidImage
from veda.config.configuration import get_image_config
from veda.constants.enums import PhotoStatus
from veda.db import sync_session
from veda.entity.config_entity import ImageConfig
from veda.integrations import realtime, storage
from veda.logging import get_logger
from veda.services import photos

log = get_logger(__name__)


class PhotoProcessingPipeline:
    def __init__(self, config: ImageConfig | None = None):
        config = config or get_image_config()
        self.validation = ImageValidation(config)
        self.processing = ImageProcessing(config)

    def run(self, photo_id: int) -> str:
        """READY, FAILED, or SKIPPED (nothing to do: already handled by another run)."""
        with sync_session() as s, s.begin():
            photo = photos.start(s, photo_id)
            if photo is None:
                return "SKIPPED"
            key, mime_type = photo.s3_key, photo.mime_type

        data = storage.get(key)  # no DB transaction is held during network or CPU work
        try:
            result = self.processing.process(self.validation.validate(data, mime_type))
        except InvalidImage as e:
            log.warning("photo_rejected", photo_id=photo_id, reason=str(e))
            with sync_session() as s, s.begin():
                photos.fail(s, photo_id, str(e))
            return PhotoStatus.FAILED

        keys = {}
        for v in result.variants:
            keys[v.name] = storage.variant_key(key, v.name, v.format.lower())
            storage.put(keys[v.name], v.data, v.content_type)

        with sync_session() as s, s.begin():
            ready = photos.finish(s, photo_id, result, keys)
            fields = photos.live_fields(ready, keys) if ready else None
        if fields is None:
            return "SKIPPED"
        realtime.publish_photo_ready(photo.event_id, fields)  # after commit: guests can load it
        return PhotoStatus.READY
