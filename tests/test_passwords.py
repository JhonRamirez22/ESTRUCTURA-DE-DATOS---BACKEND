import pytest

from app.core.passwords import hash_password, verify_password
from app.schemas.auth import AccountCredentials


def test_passwords_use_unique_salts_and_verify_unicode_without_trimming() -> None:
    password = "  Contraseña personal de Pasto  "
    encoded = hash_password(password)
    assert encoded != hash_password(password)
    assert password not in encoded
    assert verify_password(password, encoded)
    assert not verify_password(password.strip(), encoded)
    assert not verify_password("otra contraseña", encoded)
    assert not verify_password(password, "invalid")


def test_account_validation_does_not_strip_passwords_and_rejects_short_values() -> None:
    credentials = AccountCredentials.model_validate(
        {
            "email": " Despacho@Example.com ",
            "password": " contraseña con espacios ",
        }
    )
    assert credentials.email == "despacho@example.com"
    assert credentials.password.get_secret_value() == " contraseña con espacios "
    for password in ("short", "123456789012345", "x" * 129):
        with pytest.raises(ValueError):
            AccountCredentials.model_validate({"email": "a@example.com", "password": password})
