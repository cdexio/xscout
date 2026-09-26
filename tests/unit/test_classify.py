from datetime import UTC, datetime

import pytest

from xscout.gateway.classify import Kind, Scope, classify
from xscout.store.models import AccountStatus
from xscout.xweb.errors import Inspected, RateInfo, XError

CD = dict(
    unknown_cooldown_sec=900, rate_abuse_cooldown_sec=3600, cloudflare_cooldown_sec=900, transient_cooldown_sec=30
)


def ins(status=200, codes=(), remaining=None, has_data=True, is_html=False, body=True):
    return Inspected(
        status=status,
        body={} if body else None,
        errors=[XError(c, f"msg {c}") for c in codes],
        rate=RateInfo(50, remaining, datetime(2026, 9, 26, tzinfo=UTC) if remaining is not None else None),
        is_html=is_html,
        has_data=has_data,
    )


@pytest.mark.parametrize(
    "inspected,kind,scope,status",
    [
        (ins(), Kind.OK, Scope.NONE, None),
        (ins(has_data=False), Kind.EMPTY, Scope.NONE, None),
        (ins(codes=[214], has_data=False), Kind.CALLER_ERROR, Scope.NONE, None),
        (ins(429, remaining=0, has_data=False), Kind.QUOTA, Scope.BUCKET, None),
        (ins(429, remaining=None, has_data=False), Kind.QUOTA, Scope.BUCKET, None),
        (ins(429, codes=[88], remaining=12, has_data=False), Kind.RATE_ABUSE, Scope.ACCOUNT, None),
        (ins(403, codes=[326], has_data=False), Kind.LOCKED, Scope.ACCOUNT, AccountStatus.LOCKED),
        (ins(401, codes=[32], has_data=False), Kind.EXPIRED, Scope.ACCOUNT, AccountStatus.EXPIRED),
        (ins(403, codes=[353], has_data=False), Kind.EXPIRED, Scope.ACCOUNT, AccountStatus.EXPIRED),
        (ins(403, codes=[64], has_data=False), Kind.SUSPENDED, Scope.ACCOUNT, AccountStatus.SUSPENDED),
        (ins(404, has_data=False, body=False), Kind.NOT_FOUND_OP, Scope.BUCKET, None),
        (ins(403, has_data=False, is_html=True, body=False), Kind.CLOUDFLARE, Scope.ACCOUNT, None),
        (ins(503, has_data=False, body=False), Kind.TRANSIENT, Scope.BUCKET, None),
        (ins(200, codes=[336], has_data=False), Kind.FEATURES, Scope.BUCKET, None),
        (ins(200, codes=[999], has_data=False), Kind.UNKNOWN, Scope.BUCKET, None),
        (ins(418, has_data=False), Kind.UNKNOWN, Scope.BUCKET, None),
    ],
)
def test_classification_table(inspected, kind, scope, status):
    v = classify(inspected, **CD)
    assert v.kind is kind and v.cooldown_scope is scope and v.new_status == status


def test_quota_cools_until_reset_and_abuse_for_an_hour():
    assert classify(ins(429, remaining=0, has_data=False), **CD).cooldown_sec is None
    assert classify(ins(429, codes=[88], remaining=3, has_data=False), **CD).cooldown_sec == 3600


def test_success_and_caller_errors_never_retry():
    assert classify(ins(), **CD).success
    v = classify(ins(codes=[214], has_data=False), **CD)
    assert not v.retry_other_account and not v.success


def test_404_and_336_ask_for_registry_refresh():
    assert classify(ins(404, has_data=False, body=False), **CD).refresh_registry
    assert classify(ins(200, codes=[336], has_data=False), **CD).refresh_registry
