import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, connection
from django.test import Client, override_settings
from django.utils import timezone

from apps.authentication.mail import DeliveryError
from apps.organization.models import Area
from apps.workers.models import Worker, WorkerArea
from apps.workers.services.areas import authorized_areas

from apps.authentication.models import (
    AuthThrottle,
    AuthToken,
    Invitation,
    MigrationBackup,
    RecoveryRequest,
    Role,
    User,
    UserRole,
)
from apps.projects.services.access import can_access_project, can_supervise_project
from apps.authentication.tokens import decode_token, digest_token


class AuthenticationFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("AUTH_TEST_SQLITE") != "1" or connection.vendor != "sqlite":
            raise unittest.SkipTest("Integration tests require isolated SQLite")
        cls.password_settings = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
        cls.password_settings.enable()
        cls.tables = [
            Area,
            Worker,
            WorkerArea,
            User,
            Role,
            UserRole,
            AuthThrottle,
            AuthToken,
            RecoveryRequest,
            Invitation,
            MigrationBackup,
        ]
        with connection.schema_editor() as editor:
            for model in cls.tables:
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(cls.tables):
                editor.delete_model(model)
        cls.password_settings.disable()

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/api/auth/csrf/")
        self.csrf = self.client.cookies["csrftoken"].value
        self.area = Area.objects.create(code="ING", name="Ingeniería", active=True)
        for code, name in (("ADMINISTRADOR", "Administrador"), ("TRABAJADOR", "Trabajador")):
            Role.objects.create(code=code, name=name)

    def tearDown(self):
        with connection.cursor() as cursor:
            for model in reversed(self.tables):
                cursor.execute(f'DELETE FROM "{model._meta.db_table}"')

    def worker(self, code, email):
        return Worker.objects.create(
            area=self.area, code=code, first_names="Persona", last_names=code, email=email, active=True
        )

    def account(self, code, email, role, active=True, password="StrongPassword!2026"):
        worker = self.worker(code, email)
        user = User.objects.create(
            worker=worker, username=f"gm_{code}", password=make_password(password), is_active=active
        )
        UserRole.objects.create(user=user, role_code=role)
        return user

    def post(self, path, payload=None, bearer=None):
        headers = {"HTTP_X_CSRFTOKEN": self.csrf}
        if bearer:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
        return self.client.post(path, data=json.dumps(payload or {}), content_type="application/json", **headers)

    def login(self, email, password="StrongPassword!2026"):
        return self.post("/api/auth/login/", {"email": email, "password": password})

    def invite_data(self, email="new@gmail.com", **changes):
        return {
            "first_names": "Nombre Sintético",
            "last_names": "Apellido Prueba",
            "email": email,
            "role": "TRABAJADOR",
            "all_areas": False,
            "area_ids": [self.area.pk],
            **changes,
        }

    def admin_access(self):
        self.account("ADMIN", "admin@example.com", "ADMINISTRADOR")
        return self.login("admin@example.com").json()["access"]

    def password_data(self, token, password="StrongPassword!2026", **changes):
        return {"token": token, "password": password, "password_confirmation": password, **changes}

    def test_login_refresh_logout_and_replay(self):
        self.account("T01", "t01@example.com", "TRABAJADOR")
        login = self.login("t01@example.com")
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.cookies["gm_refresh"]["httponly"])
        access = login.json()["access"]
        self.assertEqual(self.client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {access}").status_code, 200)
        self.assertEqual(self.post("/api/auth/admin/invitations/", self.invite_data(), access).status_code, 403)
        for path in ("areas", "accounts", "recovery-requests"):
            self.assertEqual(
                self.client.get(f"/api/auth/admin/{path}/", HTTP_AUTHORIZATION=f"Bearer {access}").status_code, 403
            )
        self.assertEqual(self.post("/api/auth/admin/accounts/999/resend-invitation/", bearer=access).status_code, 403)
        self.assertEqual(self.post("/api/auth/admin/recovery-requests/999/issue/", bearer=access).status_code, 403)
        old_refresh = self.client.cookies["gm_refresh"].value
        renewed = self.post("/api/auth/refresh/")
        self.assertEqual(renewed.status_code, 200)
        self.assertNotEqual(self.client.cookies["gm_refresh"].value, old_refresh)
        self.assertEqual(
            decode_token(old_refresh, "refresh")["exp"],
            decode_token(self.client.cookies["gm_refresh"].value, "refresh")["exp"],
        )
        self.client.cookies["gm_refresh"] = old_refresh
        self.assertEqual(self.post("/api/auth/refresh/").status_code, 401)
        self.assertEqual(self.login("t01@example.com").status_code, 200)
        latest_access = self.login("t01@example.com").json()["access"]
        self.assertEqual(self.post("/api/auth/logout/").status_code, 200)
        self.assertEqual(self.post("/api/auth/refresh/").status_code, 401)
        self.assertEqual(
            self.client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {latest_access}").status_code, 401
        )

    def test_inactive_and_unknown_role_cannot_login(self):
        self.account("T02", "t02@example.com", "TRABAJADOR", active=False)
        self.assertEqual(self.login("t02@example.com").status_code, 401)
        user = self.account("T03", "t03@example.com", "TRABAJADOR")
        UserRole.objects.filter(user=user).update(role_code="GERENCIA")
        self.assertEqual(self.login("t03@example.com").status_code, 401)

    @patch("apps.authentication.views.send_link", return_value="synthetic-reset-id")
    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-invite-id")
    def test_admin_invitation_activation_and_reset(self, send_link, reset_mail):
        admin_access = self.admin_access()
        invited = self.post("/api/auth/admin/invitations/", self.invite_data("t04@gmail.com"), admin_access)
        self.assertEqual(invited.status_code, 201)
        worker = Worker.objects.get(email="t04@gmail.com")
        account = User.objects.get(worker=worker)
        self.assertEqual(account.username, "t04@gmail.com")
        self.assertFalse(account.is_active)
        first_token = send_link.call_args.args[3]
        self.assertEqual(
            self.post(f"/api/auth/admin/accounts/{account.pk}/resend-invitation/", bearer=admin_access).status_code, 200
        )
        activation_token = send_link.call_args.args[3]
        self.assertEqual(
            self.post(
                "/api/auth/activate/",
                {
                    "token": first_token,
                    "password": "StrongPassword!2026",
                    "password_confirmation": "StrongPassword!2026",
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post(
                "/api/auth/activate/", {"token": activation_token, "password": "123", "password_confirmation": "123"}
            ).status_code,
            400,
        )
        activated = self.post(
            "/api/auth/activate/",
            {
                "token": activation_token,
                "password": "StrongPassword!2026",
                "password_confirmation": "StrongPassword!2026",
            },
        )
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(
            self.post(
                "/api/auth/activate/",
                {
                    "token": activation_token,
                    "password": "StrongPassword!2026",
                    "password_confirmation": "StrongPassword!2026",
                },
            ).status_code,
            400,
        )
        self.assertEqual(self.login("t04@gmail.com").status_code, 200)
        worker_refresh = self.client.cookies["gm_refresh"].value
        unknown = self.post("/api/auth/recovery-requests/", {"email": "unknown@example.com"})
        known = self.post("/api/auth/recovery-requests/", {"email": "t04@gmail.com"})
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.json(), known.json())
        pending = RecoveryRequest.objects.get(user=account)
        issued = self.post(f"/api/auth/admin/recovery-requests/{pending.pk}/issue/", bearer=admin_access)
        self.assertEqual(issued.status_code, 200)
        reset_token = reset_mail.call_args.args[3]
        reset = self.post(
            "/api/auth/reset/",
            {"token": reset_token, "password": "OtherStrong!2026", "password_confirmation": "OtherStrong!2026"},
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(
            self.post(
                "/api/auth/reset/",
                {"token": reset_token, "password": "OtherStrong!2026", "password_confirmation": "OtherStrong!2026"},
            ).status_code,
            400,
        )
        self.client.cookies["gm_refresh"] = worker_refresh
        self.assertEqual(self.post("/api/auth/refresh/").status_code, 401)
        self.assertEqual(self.login("t04@gmail.com", "StrongPassword!2026").status_code, 401)
        self.assertEqual(self.login("t04@gmail.com", "OtherStrong!2026").status_code, 200)

    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-message-id")
    def test_specific_areas_registration_and_readonly_activation(self, send_link):
        access = self.admin_access()
        second = Area.objects.create(code="AMB", name="Ambiente", active=True)
        response = self.post(
            "/api/auth/admin/invitations/", self.invite_data(area_ids=[second.pk, self.area.pk]), access
        )
        self.assertEqual(response.status_code, 201)
        user = User.objects.select_related("worker").get(pk=response.json()["account_id"])
        self.assertEqual(user.worker.area_id, second.pk)
        self.assertFalse(user.worker.all_areas)
        self.assertEqual(set(authorized_areas(user.worker).values_list("id", flat=True)), {self.area.pk, second.pk})
        self.assertEqual(WorkerArea.objects.filter(worker=user.worker).count(), 2)
        self.assertLessEqual(len(user.worker.code), 30)
        self.assertLessEqual(len(user.username), 80)
        self.assertFalse(user.has_usable_password())
        token = send_link.call_args.args[3]
        preview = self.post("/api/auth/activate/preview/", {"token": token})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["email"], "new@gmail.com")
        self.assertEqual(preview.json()["role"], "TRABAJADOR")
        self.assertEqual(len(preview.json()["areas"]), 2)
        for field, value in (
            ("email", "changed@gmail.com"),
            ("role", "ADMINISTRADOR"),
            ("area_ids", []),
            ("first_names", "Changed"),
        ):
            self.assertEqual(
                self.post("/api/auth/activate/", self.password_data(token, **{field: value})).status_code, 400
            )
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(self.post("/api/auth/activate/validate-password/", self.password_data(token)).status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(self.post("/api/auth/activate/", self.password_data(token)).status_code, 200)

    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-message-id")
    def test_all_areas_is_dynamic_and_never_uses_a_primary_area(self, send_link):
        access = self.admin_access()
        inactive = Area.objects.create(code="OFF", name="Inactiva", active=False)
        response = self.post("/api/auth/admin/invitations/", self.invite_data(all_areas=True, area_ids=[]), access)
        self.assertEqual(response.status_code, 201)
        user = User.objects.select_related("worker").get(pk=response.json()["account_id"])
        self.assertIsNone(user.worker.area_id)
        self.assertTrue(user.worker.all_areas)
        self.assertFalse(WorkerArea.objects.filter(worker=user.worker).exists())
        future = Area.objects.create(code="FUT", name="Área futura", active=True)
        self.assertEqual(set(authorized_areas(user.worker).values_list("id", flat=True)), {self.area.pk, future.pk})
        inactive.active = True
        inactive.save()
        self.assertTrue(authorized_areas(user.worker).filter(pk=inactive.pk).exists())
        area_list = self.client.get("/api/auth/admin/areas/", HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(len(area_list.json()), 3)
        preview = self.post("/api/auth/activate/preview/", {"token": send_link.call_args.args[3]}).json()
        self.assertTrue(preview["all_areas"])
        self.assertEqual(len(preview["areas"]), 3)

    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-message-id")
    def test_invalid_registration_is_rejected_without_partial_records(self, send_link):
        access = self.admin_access()
        inactive = Area.objects.create(code="OFF", name="Inactiva", active=False)
        invalid = [
            {"area_ids": []},
            {"area_ids": [self.area.pk, self.area.pk]},
            {"area_ids": [inactive.pk]},
            {"area_ids": [999999]},
            {"all_areas": True},
            {"role": "COORDINADOR"},
            {"role": "COLABORADOR"},
            {"role": "GERENCIA"},
            {"first_names": " "},
            {"last_names": " "},
            {"email": "bad"},
            {"email": "new@example.com"},
            {"worker_id": 999},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.post("/api/auth/admin/invitations/", self.invite_data(**changes), access)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Worker.objects.count(), 1)
                self.assertEqual(User.objects.count(), 1)
        send_link.assert_not_called()
        with patch(
            "apps.authentication.services.accounts.UserRole.objects.create",
            side_effect=IntegrityError("synthetic constraint"),
        ):
            with self.assertRaises(IntegrityError):
                self.post("/api/auth/admin/invitations/", self.invite_data(), access)
        self.assertEqual(Worker.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(WorkerArea.objects.count(), 0)

    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-message-id")
    def test_max_length_gmail_is_preserved_and_duplicates_are_rejected(self, send_link):
        access = self.admin_access()
        email = "a" * 64 + "@gmail.com"
        response = self.post("/api/auth/admin/invitations/", self.invite_data(email), access)
        self.assertEqual(response.status_code, 201)
        worker = Worker.objects.get(email=email)
        self.assertEqual(worker.user.username, email)
        self.assertEqual(
            self.post("/api/auth/admin/invitations/", self.invite_data(email.upper()), access).status_code, 400
        )
        self.assertEqual(
            self.post("/api/auth/activate/", self.password_data(send_link.call_args.args[3])).status_code, 200
        )
        self.assertEqual(self.login(email.upper()).status_code, 200)
        self.assertEqual(Worker.objects.filter(email=email).count(), 1)

    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-message-id")
    def test_activation_validates_password_and_email_binding(self, send_link):
        access = self.admin_access()
        self.post("/api/auth/admin/invitations/", self.invite_data(), access)
        token = send_link.call_args.args[3]
        invalid_passwords = ["short", "password123456", "new@gmail.com", "Nombre Sintético"]
        for password in invalid_passwords:
            self.assertEqual(
                self.post("/api/auth/activate/validate-password/", self.password_data(token, password)).status_code, 400
            )
            self.assertEqual(self.post("/api/auth/activate/", self.password_data(token, password)).status_code, 400)
        self.assertEqual(
            self.post(
                "/api/auth/activate/", self.password_data(token, password_confirmation="Mismatch!2026")
            ).status_code,
            400,
        )
        Worker.objects.filter(email="new@gmail.com").update(email="changed@gmail.com")
        self.assertEqual(self.post("/api/auth/activate/preview/", {"token": token}).status_code, 400)
        self.assertEqual(self.post("/api/auth/activate/", self.password_data(token)).status_code, 400)

    @patch("apps.authentication.services.accounts.send_link", return_value="synthetic-message-id")
    def test_expired_activation_and_inactive_worker_cannot_activate(self, send_link):
        access = self.admin_access()
        self.post("/api/auth/admin/invitations/", self.invite_data(), access)
        token = send_link.call_args.args[3]
        AuthToken.objects.filter(digest=digest_token(token)).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post("/api/auth/activate/", self.password_data(token)).status_code, 400)
        user = User.objects.get(worker__email="new@gmail.com")
        self.post(f"/api/auth/admin/accounts/{user.pk}/resend-invitation/", bearer=access)
        token = send_link.call_args.args[3]
        Worker.objects.filter(pk=user.worker_id).update(active=False)
        self.assertEqual(self.post("/api/auth/activate/", self.password_data(token)).status_code, 400)

    @patch("apps.authentication.services.accounts.send_link")
    def test_mail_failure_keeps_pending_account_and_retry_does_not_duplicate(self, send_link):
        access = self.admin_access()
        send_link.side_effect = DeliveryError("synthetic rejection")
        response = self.post("/api/auth/admin/invitations/", self.invite_data(), access)
        self.assertEqual(response.status_code, 503)
        user = User.objects.get(worker__email="new@gmail.com")
        failure = response.json()
        self.assertEqual(failure["account_id"], str(user.pk))
        self.assertEqual(failure["delivery_status"], "failed")
        self.assertIn("rechazó", failure["detail"])
        self.assertFalse(user.is_active)
        self.assertEqual(user.invitation.delivery_status, "failed")
        first_token = send_link.call_args.args[3]
        self.assertEqual(self.post("/api/auth/activate/preview/", {"token": first_token}).status_code, 400)
        send_link.side_effect = DeliveryError("synthetic timeout", "unknown")
        response = self.post(f"/api/auth/admin/accounts/{user.pk}/resend-invitation/", bearer=access)
        self.assertEqual(response.status_code, 503)
        failure = response.json()
        self.assertEqual(failure["account_id"], str(user.pk))
        self.assertEqual(failure["delivery_status"], "unknown")
        self.assertIn("no se confirmó", failure["detail"])
        user.invitation.refresh_from_db()
        self.assertEqual(user.invitation.delivery_status, "unknown")
        uncertain_token = send_link.call_args.args[3]
        send_link.side_effect = None
        send_link.return_value = "synthetic-accepted-id"
        self.assertEqual(
            self.post(f"/api/auth/admin/accounts/{user.pk}/resend-invitation/", bearer=access).status_code, 200
        )
        user.invitation.refresh_from_db()
        self.assertEqual(user.invitation.delivery_status, "accepted")
        self.assertEqual(user.invitation.provider_message_id, "synthetic-accepted-id")
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Worker.objects.count(), 2)
        self.assertEqual(self.post("/api/auth/activate/preview/", {"token": uncertain_token}).status_code, 400)
        self.assertEqual(
            self.post(
                f"/api/auth/admin/accounts/{user.pk}/resend-invitation/", {"email": "changed@gmail.com"}, access
            ).status_code,
            400,
        )

    @patch("apps.authentication.services.accounts.send_link")
    def test_unconfirmed_invitation_delivery_is_retried_once(self, send_link):
        access = self.admin_access()
        send_link.side_effect = [DeliveryError("synthetic timeout", "unknown"), "synthetic-accepted-id"]
        response = self.post("/api/auth/admin/invitations/", self.invite_data(), access)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["delivery_status"], "accepted")
        self.assertEqual(send_link.call_count, 2)
        self.assertEqual(send_link.call_args_list[0].args[3], send_link.call_args_list[1].args[3])

    def test_admin_global_scope_and_worker_membership_assignment_restrictions(self):
        admin = self.account("ADMIN", "admin@example.com", "ADMINISTRADOR")
        worker = self.account("WORK", "worker@example.com", "TRABAJADOR")
        other = Area.objects.create(code="OTHER", name="Otra", active=True)
        self.assertTrue(can_access_project(admin, other.pk, is_member=False, is_assigned=False))
        self.assertTrue(can_supervise_project(admin, other.pk, is_member=False, project_role=None))
        self.assertFalse(can_access_project(worker, self.area.pk, is_member=False, is_assigned=True))
        self.assertFalse(can_access_project(worker, self.area.pk, is_member=True, is_assigned=False))
        self.assertFalse(can_access_project(worker, other.pk, is_member=True, is_assigned=True))
        self.assertTrue(can_access_project(worker, self.area.pk, is_member=True, is_assigned=True))
        self.assertFalse(can_supervise_project(worker, self.area.pk, is_member=True, project_role="COLABORADOR"))
        self.assertTrue(can_supervise_project(worker, self.area.pk, is_member=True, project_role="RESPONSABLE"))
        worker.worker.all_areas = True
        worker.worker.area = None
        worker.worker.save()
        self.assertTrue(can_access_project(worker, other.pk, is_member=True, is_assigned=True))
        self.assertFalse(can_access_project(worker, other.pk, is_member=False, is_assigned=True))

    def test_existing_single_area_account_and_admin_survive_inactive_area(self):
        worker = self.account("WORK", "worker@example.com", "TRABAJADOR")
        admin = self.account("ADMIN", "admin@example.com", "ADMINISTRADOR")
        self.assertFalse(WorkerArea.objects.filter(worker=worker.worker).exists())
        self.assertEqual(self.login("worker@example.com").status_code, 200)
        self.area.active = False
        self.area.save()
        self.assertEqual(self.login("worker@example.com").status_code, 401)
        response = self.login("admin@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["user"]["permissions"]["manage_projects"])
        self.assertTrue(response.json()["user"]["permissions"]["supervise_projects"])
        self.assertEqual(admin.global_role, "ADMINISTRADOR")

    def test_login_attempts_are_limited_and_admin_unlocks_account(self):
        user = self.account("WORK", "worker@example.com", "TRABAJADOR")
        for _ in range(5):
            self.assertEqual(self.login("worker@example.com", "wrong").status_code, 401)
        self.assertEqual(self.login("worker@example.com").status_code, 429)
        self.assertIn("Demasiados intentos", self.login("worker@example.com").json()["detail"])
        self.assertGreaterEqual(AuthThrottle.objects.get().blocked_until - timezone.now(), timedelta(hours=23))
        access = self.admin_access()
        self.assertEqual(
            self.post(f"/api/auth/admin/accounts/{user.pk}/unlock/", bearer=access).status_code, 200
        )
        self.assertEqual(self.login("worker@example.com").status_code, 200)

    def test_daily_recovery_send_quota_is_limited(self):
        payload = {"email": "quota@gmail.com"}
        for _ in range(5):
            self.assertEqual(self.post("/api/auth/recovery-requests/", payload).status_code, 200)
        limited = self.post("/api/auth/recovery-requests/", payload)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("límite de 5 envíos", limited.json()["detail"])

    def test_explicit_relationships_are_authoritative_over_legacy_primary_area(self):
        user = self.account("WORK", "worker@example.com", "TRABAJADOR")
        explicit = Area.objects.create(code="EXP", name="Área explícita", active=True)
        WorkerArea.objects.create(worker=user.worker, area=explicit)
        self.assertEqual(list(authorized_areas(user.worker).values_list("id", flat=True)), [explicit.pk])
        self.assertFalse(can_access_project(user, self.area.pk, is_member=True, is_assigned=True))

    def test_malformed_and_extra_auth_fields_are_rejected(self):
        for path in ("login", "recovery-requests", "activate", "activate/validate-password", "reset"):
            response = self.client.post(
                f"/api/auth/{path}/", data="[]", content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.post(
                "/api/auth/login/", {"email": "worker@example.com", "password": "secret", "role": "ADMINISTRADOR"}
            ).status_code,
            400,
        )
