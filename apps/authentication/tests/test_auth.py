from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone as django_timezone
from rest_framework.exceptions import AuthenticationFailed

from apps.authentication.security import AccessTokenAuthentication, IsAdministrator
from apps.authentication.models import User
from apps.authentication.tokens import (
    digest_token as unittest_digest,
    InvalidToken,
    consume_stored,
    decode_token,
    encode_token,
    issue_access,
    next_monday_lima,
)
from apps.authentication.views import cookie_response, eligible


class TokenTests(SimpleTestCase):
    def test_access_token_rejects_expiry_and_tampering(self):
        valid = encode_token(12, "access", django_timezone.now() + timedelta(minutes=15))
        self.assertEqual(decode_token(valid, "access")["sub"], "12")
        with self.assertRaises(InvalidToken):
            decode_token(valid, "refresh")
        with self.assertRaises(InvalidToken):
            decode_token(valid[:-1] + ("A" if valid[-1] != "A" else "B"), "access")
        expired = encode_token(12, "access", django_timezone.now() - timedelta(seconds=1))
        with self.assertRaises(InvalidToken):
            decode_token(expired, "access")

    def test_refresh_expires_next_monday_lima_without_exceeding_seven_days(self):
        now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        expiry = next_monday_lima(now)
        self.assertEqual(expiry, datetime(2026, 10, 5, 5, tzinfo=timezone.utc))
        self.assertLessEqual(expiry - now, timedelta(days=7))

    def test_access_expiry_is_capped_by_session(self):
        session_expires = django_timezone.now() + timedelta(minutes=1)
        with patch("apps.authentication.tokens.issue_stored", return_value="token") as issue:
            issue_access(SimpleNamespace(pk=12), session_expires)
            self.assertLessEqual(issue.call_args.args[2], session_expires)

    def test_stored_token_cannot_be_reused(self):
        token = encode_token(12, "activation", django_timezone.now() + timedelta(hours=1))
        queryset = Mock()
        queryset.filter.return_value.update.side_effect = [1, 0]
        user = SimpleNamespace(pk=12)
        with (
            patch("apps.authentication.tokens.AuthToken.objects", queryset),
        ):
            queryset.select_related.return_value.get.return_value = SimpleNamespace(user=user, expires_at=django_timezone.now() + timedelta(hours=1))
            self.assertIs(consume_stored(token, "activation")[0], user)
            queryset.filter.assert_called_with(
                digest=unittest_digest(token), purpose="activation", user_id=12,
                consumed_at__isnull=True, expires_at__gt=queryset.filter.call_args.kwargs["expires_at__gt"],
            )
            with self.assertRaises(InvalidToken):
                consume_stored(token, "activation")
            self.assertEqual(queryset.select_related.return_value.get.call_count, 1)



class PermissionTests(SimpleTestCase):
    def test_existing_role_mapping_rejects_legacy_and_multiple_assignments(self):
        user = User(id=12)
        with patch("apps.authentication.models.UserRole.objects") as roles:
            roles.filter.return_value.values_list.return_value = ["GERENCIA"]
            self.assertIsNone(user.global_role)
            roles.filter.return_value.values_list.return_value = ["ADMINISTRADOR", "REVISOR"]
            self.assertIsNone(user.global_role)
            roles.filter.return_value.values_list.return_value = ["COLABORADOR"]
            self.assertIsNone(user.global_role)
            roles.filter.return_value.values_list.return_value = ["COORDINADOR"]
            self.assertIsNone(user.global_role)
            roles.filter.return_value.values_list.return_value = ["TRABAJADOR"]
            self.assertEqual(user.global_role, "TRABAJADOR")

    def test_unknown_and_multiple_roles_are_denied(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True, global_role=None))
        self.assertFalse(IsAdministrator().has_permission(request, None))
        request.user.global_role = "GERENCIA"
        self.assertFalse(IsAdministrator().has_permission(request, None))
        request.user.global_role = "ADMINISTRADOR"
        self.assertTrue(IsAdministrator().has_permission(request, None))

    def test_inactive_user_or_worker_is_not_eligible(self):
        worker = SimpleNamespace(active=True, area=SimpleNamespace(active=True), email="worker@example.com")
        user = SimpleNamespace(is_active=False, worker=worker, global_role="TRABAJADOR")
        self.assertFalse(eligible(user))
        user.is_active = True
        worker.active = False
        self.assertFalse(eligible(user))
        worker.active = True
        user.global_role = None
        self.assertFalse(eligible(user))

    def test_access_authentication_denies_inactive_account(self):
        user = SimpleNamespace(
            is_active=False,
            worker=SimpleNamespace(active=True, area=SimpleNamespace(active=True)),
            global_role="TRABAJADOR",
        )
        queryset = Mock()
        queryset.select_related.return_value.get.return_value = user
        request = SimpleNamespace(headers={"Authorization": "Bearer signed"})
        with (
            patch("apps.authentication.security.decode_token", return_value={"sub": "12"}),
            patch("apps.authentication.security.User.objects", queryset),
            patch("apps.authentication.security.AuthToken.objects") as tokens,
        ):
            tokens.filter.return_value.exists.return_value = True
            with self.assertRaises(AuthenticationFailed):
                AccessTokenAuthentication().authenticate(request)


class CookieTests(SimpleTestCase):
    @override_settings(AUTH_COOKIE_SECURE=True)
    def test_refresh_cookie_is_httponly_and_secure(self):
        expires = django_timezone.now() + timedelta(days=2)
        response = cookie_response({"ok": True}, "signed-refresh", expires)
        cookie = response.cookies[settings.AUTH_REFRESH_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["path"], "/api/auth/")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_refresh_and_logout_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(
            client.post(
                "/api/auth/login/", data='{"email":"a@b.com","password":"x"}', content_type="application/json"
            ).status_code,
            403,
        )
        self.assertEqual(client.post("/api/auth/refresh/").status_code, 403)
        self.assertEqual(client.post("/api/auth/logout/").status_code, 403)

    @override_settings(FRONTEND_ORIGIN="http://localhost:5173")
    def test_csrf_bootstrap_and_cors_only_allow_explicit_frontend_origin(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get("/api/auth/csrf/", HTTP_ORIGIN="http://localhost:5173")
        self.assertTrue(response.json()["csrf_token"])
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:5173")
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")
        self.assertEqual(response["Cache-Control"], "no-store")
        denied = client.get("/api/auth/csrf/", HTTP_ORIGIN="http://untrusted.example.com")
        self.assertFalse(denied.has_header("Access-Control-Allow-Origin"))
