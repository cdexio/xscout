"""Fernet encryption for account secrets (cookies, proxy URLs) at rest (spec §10)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxError(Exception):
    """Raised when a secret cannot be decrypted (wrong key or corrupted value)."""


class SecretBox:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise SecretBoxError("XSCOUT_SECRET_KEY is not a valid Fernet key") from e

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise SecretBoxError("cannot decrypt secret: wrong XSCOUT_SECRET_KEY or corrupted value") from e
