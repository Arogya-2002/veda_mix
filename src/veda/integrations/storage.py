"""S3. The database stores object keys only; URLs are signed on demand."""

import base64
import uuid
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from veda.config.configuration import get_config, get_settings

EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/heic": "heic", "image/webp": "webp"}


@lru_cache
def client() -> BaseClient:
    s = get_settings()
    return boto3.client(
        "s3",
        region_name=s.s3_region,
        endpoint_url=s.s3_endpoint_url or None,
        aws_access_key_id=s.aws_access_key_id or None,
        aws_secret_access_key=s.aws_secret_access_key or None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if s.s3_endpoint_url else "virtual"},
            request_checksum_calculation="when_required",  # we set the checksum ourselves
        ),
    )


def original_key(photographer_public_id: str, event_id: int, mime_type: str) -> str:
    # Event id, not slug: the slug can change, object keys must not.
    prefix = get_config()["storage"]["originals_prefix"]
    return (
        f"{prefix}/{photographer_public_id}/{event_id}/{uuid.uuid4().hex}.{EXTENSIONS[mime_type]}"
    )


def b64_sha256(sha256_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


def presign_put(key: str, mime_type: str, size: int, sha256_hex: str, ttl_s: int) -> dict:
    """Length, type and checksum are signed: S3 refuses any other bytes."""
    checksum = b64_sha256(sha256_hex)
    url = client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": get_settings().s3_bucket,
            "Key": key,
            "ContentType": mime_type,
            "ContentLength": size,
            "ChecksumSHA256": checksum,
        },
        ExpiresIn=ttl_s,
    )
    # The client must send exactly these headers (Content-Length is set by the HTTP library).
    return {
        "method": "PUT",
        "url": url,
        "headers": {"Content-Type": mime_type, "x-amz-checksum-sha256": checksum},
    }


def head(key: str) -> tuple[int, str | None] | None:
    """(size, base64 sha256 if S3 has one), or None if the object isn't there. Blocking."""
    try:
        r = client().head_object(Bucket=get_settings().s3_bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return r["ContentLength"], r.get("ChecksumSHA256")


def delete(key: str) -> None:
    """Idempotent: deleting a missing key succeeds. Blocking."""
    client().delete_object(Bucket=get_settings().s3_bucket, Key=key)


def variant_key(original_key: str, variant: str, extension: str) -> str:
    """originals/VP-X/12/ab12.jpg -> variants/VP-X/12/ab12_thumb.webp.

    Derived from the original's key, so a re-run overwrites instead of leaving orphans.
    """
    prefixes = get_config()["storage"]
    path = original_key.removeprefix(prefixes["originals_prefix"] + "/").rsplit(".", 1)[0]
    return f"{prefixes['variants_prefix']}/{path}_{variant.lower()}.{extension}"


def get(key: str) -> bytes:
    """The whole object (originals are at most 50 MB). Blocking."""
    return client().get_object(Bucket=get_settings().s3_bucket, Key=key)["Body"].read()


def put(key: str, data: bytes, content_type: str) -> None:
    """Server-side upload (worker output). Keys never change content, so browsers may cache
    them for good. Blocking."""
    client().put_object(
        Bucket=get_settings().s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="private, max-age=31536000, immutable",
    )
