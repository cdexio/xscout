import pytest

from xscout.crypto import SecretBox, SecretBoxError


def test_round_trip_and_ciphertext_differs():
    box = SecretBox(SecretBox.generate_key())
    token = box.encrypt("42a72933e760c9538e35810c68185e51")
    assert "42a72933" not in token
    assert box.decrypt(token) == "42a72933e760c9538e35810c68185e51"


def test_wrong_key_fails_loudly():
    token = SecretBox(SecretBox.generate_key()).encrypt("secret-value")
    with pytest.raises(SecretBoxError):
        SecretBox(SecretBox.generate_key()).decrypt(token)


def test_invalid_key_rejected():
    with pytest.raises(SecretBoxError):
        SecretBox("not-a-fernet-key")
