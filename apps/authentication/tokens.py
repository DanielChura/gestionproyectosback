import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, time, timedelta, timezone as utc_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from .models import AuthToken


class InvalidToken(Exception):
    pass


def _segment(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
    )


def _decode_segment(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def _signature(content):
    return (
        base64.urlsafe_b64encode(hmac.new(settings.AUTH_JWT_KEY.encode(), content.encode(), hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )


def encode_token(user_id, purpose, expires_at):
    now = timezone.now()
    header = _segment({"alg": "HS256", "typ": "JWT"})
    payload = _segment(
        {
            "sub": str(user_id),
            "purpose": purpose,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": secrets.token_urlsafe(24),
        }
    )
    content = f"{header}.{payload}"
    return f"{content}.{_signature(content)}"


def decode_token(token, purpose):
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError
        header, payload, signature = token.split(".")
        content = f"{header}.{payload}"
        if not hmac.compare_digest(_signature(content), signature):
            raise ValueError
        decoded_header = _decode_segment(header)
        decoded = _decode_segment(payload)
        if not isinstance(decoded, dict):
            raise ValueError
        if decoded_header != {"alg": "HS256", "typ": "JWT"} or decoded.get("purpose") != purpose:
            raise ValueError
        if not isinstance(decoded.get("exp"), int) or decoded["exp"] <= int(timezone.now().timestamp()):
            raise ValueError
        if not isinstance(decoded.get("sub"), str) or not decoded["sub"].isdigit() or not decoded.get("jti"):
            raise ValueError
        return decoded
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise InvalidToken from None


def digest_token(token):
    return hmac.new(settings.AUTH_JWT_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()


def next_monday_lima(now=None):
    current = (now or timezone.now()).astimezone(ZoneInfo("America/Lima"))
    days = (7 - current.weekday()) % 7 or 7
    monday = datetime.combine(current.date() + timedelta(days=days), time.min, ZoneInfo("America/Lima"))
    return min(monday.astimezone(utc_timezone.utc), (now or timezone.now()) + timedelta(days=7))


def issue_access(user, session_expires_at):
    expires_at = min(timezone.now() + timedelta(minutes=settings.AUTH_ACCESS_MINUTES), session_expires_at)
    return issue_stored(user, "access", expires_at)


def issue_stored(user, purpose, expires_at, revoke_previous=False):
    if revoke_previous:
        AuthToken.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
    token = encode_token(user.pk, purpose, expires_at)
    AuthToken.objects.create(user=user, purpose=purpose, digest=digest_token(token), expires_at=expires_at)
    return token


def consume_stored(token, purpose, *, user=None):
    claims = decode_token(token, purpose)
    user_id = int(claims["sub"])
    if user is not None and user.pk != user_id:
        raise InvalidToken
    now = timezone.now()
    digest = digest_token(token)
    consumed = AuthToken.objects.filter(
        digest=digest,
        purpose=purpose,
        user_id=user_id,
        consumed_at__isnull=True,
        expires_at__gt=now,
    ).update(consumed_at=now)
    if not consumed:
        raise InvalidToken
    if user is not None:
        return user, AuthToken.objects.values_list("expires_at", flat=True).get(digest=digest)
    record = AuthToken.objects.select_related("user").get(digest=digest)
    return record.user, record.expires_at


def inspect_stored(token, purpose):
    claims = decode_token(token, purpose)
    record = (
        AuthToken.objects.select_related("user", "user__worker")
        .filter(
            digest=digest_token(token),
            purpose=purpose,
            user_id=int(claims["sub"]),
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if record is None:
        raise InvalidToken
    return record
