import secrets

# No 0/O or 1/I: photographers read these IDs aloud and type them.
_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def new_photographer_public_id() -> str:
    """VP- plus 6 random characters: 32^6 ≈ 1 billion values; callers retry on collision."""
    return "VP-" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
