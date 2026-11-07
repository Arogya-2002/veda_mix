import hashlib
import hmac
import secrets

# No 0/O or 1/I: photographers read these IDs aloud and type them.
_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def new_photographer_public_id() -> str:
    """VP- plus 6 random characters: 32^6 ≈ 1 billion values; callers retry on collision."""
    return "VP-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))


def new_event_slug() -> str:
    """10 random characters (32^10 ≈ 10^15) for the gallery URL; unguessable, not the event id."""
    return "".join(secrets.choice(_ID_ALPHABET.lower()) for _ in range(10))


# scrypt from the stdlib: memory-hard like Argon2, no extra dependency.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    actual = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(actual.hex(), digest)


def new_photo_public_id() -> str:
    return "".join(secrets.choice(_ID_ALPHABET.lower()) for _ in range(16))
