import io
import os
from datetime import datetime

import pillow_heif
import pytest
from PIL import ExifTags, Image
from sqlalchemy import text

from tests.test_uploads import meta, open_session, put
from veda.components.image_processing import ImageProcessing
from veda.components.image_validation import ImageValidation, InvalidImage
from veda.config.configuration import get_image_config
from veda.db import sync_session
from veda.integrations import storage
from veda.workers.tasks import process_photo, requeue_stuck_photos

pytestmark = pytest.mark.usefixtures("s3")

CONFIG = get_image_config()
GPS = {
    ExifTags.GPS.GPSLatitudeRef: "N",
    ExifTags.GPS.GPSLatitude: (12.0, 58.0, 30.0),
    ExifTags.GPS.GPSLongitudeRef: "E",
    ExifTags.GPS.GPSLongitude: (77.0, 35.0, 10.0),
}


def jpeg(size=(4500, 3000), orientation=None, gps=True) -> bytes:
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "Canon"
    exif[ExifTags.Base.Model] = "EOS R6"
    if orientation:
        exif[ExifTags.Base.Orientation] = orientation
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2026:12:12 18:30:05"
    if gps:
        exif.get_ifd(ExifTags.IFD.GPSInfo).update(GPS)
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(buf, "JPEG", exif=exif, quality=90)
    return buf.getvalue()


def png(size=(800, 600), mode="RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (10, 20, 30, 0) if mode == "RGBA" else (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def process(data: bytes, mime="image/jpeg", config=CONFIG):
    return ImageProcessing(config).process(ImageValidation(config).validate(data, mime))


def decoded(variant) -> Image.Image:
    image = Image.open(io.BytesIO(variant.data))
    image.load()
    return image


# --- components ----------------------------------------------------------------------


def test_jpeg_becomes_rotated_webp_copies_without_metadata():
    source = jpeg(orientation=6)  # camera held upright: stored landscape, shown portrait
    assert Image.open(io.BytesIO(source)).getexif().get_ifd(ExifTags.IFD.GPSInfo)  # has GPS

    result = process(source)

    assert (result.width, result.height) == (3000, 4500)
    assert result.taken_at == datetime(2026, 12, 12, 18, 30, 5)
    assert (result.camera_make, result.camera_model) == ("Canon", "EOS R6")
    web, thumb = result.variants
    assert (web.name, thumb.name) == ("WEB", "THUMB")
    for variant, long_edge in ((web, CONFIG.web_px), (thumb, CONFIG.thumb_px)):
        image = decoded(variant)
        assert image.format == "WEBP" and variant.content_type == "image/webp"
        assert max(image.size) == long_edge and image.height > image.width  # rotated
        assert (image.width, image.height) == (variant.width, variant.height)
        assert len(image.getexif()) == 0 and "xmp" not in image.info  # GPS gone


def test_png_keeps_transparency():
    web, _ = process(png(), "image/png").variants
    assert decoded(web).mode == "RGBA"


def test_small_image_is_not_enlarged():
    result = process(jpeg(size=(300, 200), gps=False))
    web, thumb = result.variants
    assert (web.width, web.height) == (300, 200) == (thumb.width, thumb.height)


def test_heic_is_processed():
    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (1200, 800), (90, 90, 200))).save(buf, quality=60)
    result = process(buf.getvalue(), "image/heic")
    assert (result.width, result.height) == (1200, 800)
    assert decoded(result.variants[0]).format == "WEBP"


def test_missing_exif_gives_empty_metadata():
    result = process(png(mode="RGB"), "image/png")
    assert (result.taken_at, result.camera_make, result.camera_model) == (None, None, None)


@pytest.mark.parametrize(
    ("data", "mime", "reason"),
    [
        (os.urandom(5000), "image/jpeg", "Not a readable image"),
        (png(), "image/jpeg", "File content is PNG, not image/jpeg"),
        (jpeg(gps=False), "image/heic", "File content is JPEG, not image/heic"),
        (jpeg(gps=False)[:3000], "image/jpeg", "Image data is damaged"),  # truncated
    ],
)
def test_bad_files_are_rejected(data, mime, reason):
    with pytest.raises(InvalidImage, match=reason):
        process(data, mime)


def test_too_many_pixels_rejected_before_decoding():
    small_limit = CONFIG.__class__(**(CONFIG.__dict__ | {"max_pixels": 1000 * 1000}))
    with pytest.raises(InvalidImage, match="above the pixel limit"):
        process(jpeg(size=(2000, 1000), gps=False), config=small_limit)


# --- worker, end to end ----------------------------------------------------------------


def uploaded(client, headers, data, mime="image/jpeg") -> int:
    """Upload through the API; returns the new photo's id."""
    _, sid = open_session(client, headers)
    r = client.post(
        f"/upload-sessions/{sid}/files", json={"files": [meta(data, mime=mime)]}, headers=headers
    )
    decision = r.json()[0]
    put(decision["upload"], data)
    r = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers)
    assert r.status_code == 200, r.text
    with sync_session() as s:
        return s.execute(
            text("SELECT id FROM photos WHERE public_id = :p"), {"p": r.json()["public_id"]}
        ).scalar_one()


def state(photo_id: int) -> dict:
    with sync_session() as s:
        photo = (
            s.execute(text("SELECT * FROM photos WHERE id = :id"), {"id": photo_id})
            .mappings()
            .one()
        )
        job = (
            s.execute(text("SELECT * FROM processing_jobs WHERE photo_id = :id"), {"id": photo_id})
            .mappings()
            .one()
        )
        variants = (
            s.execute(
                text("SELECT * FROM photo_variants WHERE photo_id = :id ORDER BY variant"),
                {"id": photo_id},
            )
            .mappings()
            .all()
        )
    return {"photo": dict(photo), "job": dict(job), "variants": [dict(v) for v in variants]}


def test_upload_is_queued_then_worker_makes_it_ready(client, active, s3, enqueued):
    _, headers = active()
    photo_id = uploaded(client, headers, jpeg(orientation=6))
    assert enqueued == [photo_id]
    before = state(photo_id)
    assert before["photo"]["status"] == "PROCESSING" and before["job"]["status"] == "QUEUED"

    process_photo.apply(args=[photo_id])

    after = state(photo_id)
    photo, job = after["photo"], after["job"]
    assert photo["status"] == "READY" and photo["ready_at"] is not None
    assert (photo["width"], photo["height"]) == (3000, 4500)
    assert photo["taken_at"] == datetime(2026, 12, 12, 18, 30, 5)
    assert (photo["camera_make"], photo["camera_model"]) == ("Canon", "EOS R6")
    assert (job["status"], job["attempts"], job["last_error"]) == ("SUCCEEDED", 1, None)

    thumb, web = after["variants"]
    assert (thumb["variant"], web["variant"]) == ("THUMB", "WEB")
    for v in (thumb, web):
        assert v["s3_key"].startswith("variants/") and v["s3_key"].endswith(".webp")
        head = s3.head_object(Bucket="veda-test", Key=v["s3_key"])
        assert head["ContentLength"] == v["size_bytes"]
        assert head["ContentType"] == "image/webp"


def test_duplicate_queue_message_is_skipped(client, active, s3):
    _, headers = active()
    photo_id = uploaded(client, headers, jpeg())
    process_photo.apply(args=[photo_id])
    assert process_photo.apply(args=[photo_id]).get() == "SKIPPED"
    after = state(photo_id)
    assert len(after["variants"]) == 2 and after["job"]["attempts"] == 1


def test_not_an_image_fails_without_retrying(client, active, s3):
    _, headers = active()
    photo_id = uploaded(client, headers, os.urandom(4000))  # declared image/jpeg
    assert process_photo.apply(args=[photo_id]).get() == "FAILED"
    after = state(photo_id)
    assert after["photo"]["status"] == "FAILED" and after["variants"] == []
    assert (after["job"]["status"], after["job"]["attempts"]) == ("FAILED", 1)
    assert after["job"]["last_error"] == "Not a readable image"


def test_storage_hiccup_is_retried(client, active, s3, monkeypatch):
    _, headers = active()
    photo_id = uploaded(client, headers, jpeg())
    real_get, calls = storage.get, []

    def flaky_get(key):
        calls.append(key)
        if len(calls) == 1:
            raise ConnectionError("S3 timed out")
        return real_get(key)

    monkeypatch.setattr(storage, "get", flaky_get)
    process_photo.apply(args=[photo_id])
    after = state(photo_id)
    assert after["photo"]["status"] == "READY"
    assert (after["job"]["status"], after["job"]["attempts"]) == ("SUCCEEDED", 2)


def test_gives_up_after_max_attempts(client, active, s3, monkeypatch):
    _, headers = active()
    photo_id = uploaded(client, headers, jpeg())

    def down(key):
        raise ConnectionError("S3 down")

    monkeypatch.setattr(storage, "get", down)
    process_photo.apply(args=[photo_id])
    after = state(photo_id)
    assert after["photo"]["status"] == "FAILED"
    assert (after["job"]["status"], after["job"]["attempts"]) == ("FAILED", 3)
    assert after["job"]["last_error"] == "ConnectionError: S3 down"


def test_sweep_requeues_lost_jobs_and_gives_up_on_dead_ones(client, active, s3, monkeypatch):
    _, headers = active()
    lost = uploaded(client, headers, jpeg())  # queue message never arrived
    dead = uploaded(client, headers, jpeg(size=(4000, 3000)))  # worker died 3 times
    fresh = uploaded(client, headers, jpeg(size=(3000, 3000)))  # just queued: leave alone
    with sync_session() as s, s.begin():
        s.execute(
            text(
                "UPDATE processing_jobs SET queued_at = now() - interval '1 hour' WHERE photo_id = :id"
            ),
            {"id": lost},
        )
        s.execute(
            text(
                "UPDATE processing_jobs SET status = 'RUNNING', attempts = 3, "
                "started_at = now() - interval '1 hour' WHERE photo_id = :id"
            ),
            {"id": dead},
        )
    queued: list[int] = []
    monkeypatch.setattr(process_photo, "delay", queued.append)

    assert requeue_stuck_photos.apply().get() == 1

    assert queued == [lost]
    assert state(lost)["job"]["status"] == "QUEUED"
    assert state(dead)["photo"]["status"] == "FAILED"
    assert state(dead)["job"]["last_error"] == "Gave up after 3 attempts"
    assert state(fresh)["job"]["status"] == "QUEUED"
