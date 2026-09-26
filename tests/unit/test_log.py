import json
import logging

from xscout.log import REDACTED, RedactingJsonFormatter, Redactor, redactor


def test_registered_secret_is_redacted():
    r = Redactor()
    r.register("abcdef0123456789")
    assert r.redact("value abcdef0123456789 end") == f"value {REDACTED} end"


def test_secret_shaped_fragments_are_redacted_without_registration():
    r = Redactor()
    text = (
        'auth_token=deadbeefdeadbeef; ct0=cafebabecafebabe {"x-csrf-token": "cafebabe99"} '
        "authorization: Bearer AAAAtoken%3Dxyz"
    )
    out = r.redact(text)
    for leaked in ("deadbeefdeadbeef", "cafebabecafebabe", "cafebabe99", "AAAAtoken%3Dxyz"):
        assert leaked not in out
    assert out.count(REDACTED) == 4


def test_short_values_are_not_registered():
    r = Redactor()
    r.register("abc", None, "")
    assert r.redact("abc") == "abc"


def test_json_formatter_redacts_message_and_fields():
    redactor.register("0123456789abcdef-secret")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "cookie 0123456789abcdef-secret", None, None)
    record.fields = {"header": "ct0=ffffffffffffffff"}
    payload = json.loads(RedactingJsonFormatter().format(record))
    assert "0123456789abcdef-secret" not in payload["msg"]
    assert "ffffffffffffffff" not in payload["header"]
    assert payload["level"] == "info"
