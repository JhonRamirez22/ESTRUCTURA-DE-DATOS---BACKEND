"""PBKDF2 de la biblioteca estándar; no almacena contraseñas recuperables."""

import hashlib
import hmac
import secrets

PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2-sha256:{PASSWORD_ITERATIONS}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, digest = encoded.split(":")
        if algorithm != "pbkdf2-sha256" or int(rounds) != PASSWORD_ITERATIONS:
            return False
        salt_bytes, expected = bytes.fromhex(salt), bytes.fromhex(digest)
        if len(salt_bytes) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
