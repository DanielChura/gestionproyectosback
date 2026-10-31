import json
import os
import unittest
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.teams.models import Team, TeamMember
from apps.authentication.models import AuthThrottle, AuthToken, Invitation, RecoveryRequest, Role, User, UserRole
from apps.authentication.tokens import InvalidToken, consume_stored, digest_token, issue_access, issue_stored
from apps.organization.models import Area, Specialty
from apps.workers.models import Worker, WorkerArea
from apps.workers.services.areas import area_summaries, area_summary
from apps.projects.models import (
    Project, ProjectFavorite, ProjectMember, ProjectRequirement, ProjectState, ProjectTaskStatus, ProjectType,
    Task, TaskAssignee, TaskDependency, TaskLabel, TaskState, TaskTransition, TaskStateDefinition, TaskStatusTemplate, TaskActivity, TaskRelation,
    ProjectTeam, TaskOrder,
)
from . import test_projects


class PerformanceRegressionTests(unittest.TestCase):
    tables = [
        Area, Specialty, Worker, WorkerArea, Team, TeamMember, User, Role, UserRole, AuthToken, Invitation, RecoveryRequest, AuthThrottle,
        ProjectType, ProjectState, TaskState, TaskTransition, Project, ProjectFavorite, ProjectMember, ProjectRequirement,
        ProjectTaskStatus, TaskStateDefinition, TaskStatusTemplate, Task, TaskAssignee, TaskDependency, TaskLabel, TaskRelation, TaskActivity,
        ProjectTeam, TaskOrder,
    ]

    @classmethod
    def setUpClass(cls):
        isolated_mysql = (
            connection.vendor == "mysql"
            and connection.settings_dict["HOST"] == "127.0.0.1"
            and connection.settings_dict["NAME"] == "gm_test_single_area"
        )
        isolated_sqlite = connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
        if os.environ.get("AUTH_TEST_SQLITE") != "1" or not (isolated_mysql or isolated_sqlite):
            raise RuntimeError("Se requiere la configuración de pruebas aislada")
        if connection.introspection.table_names():
            raise RuntimeError("La base aislada debe estar vacía; no se eliminarán tablas existentes")
        with connection.schema_editor() as editor:
            for model in cls.tables:
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(cls.tables):
                editor.delete_model(model)

    def setUp(self):
        self.atomic = transaction.atomic()
        self.atomic.__enter__()
        self.fixture = test_projects.ProjectFlowTests()
        self.fixture.setUp()
        self.tokens = {
            name: issue_access(getattr(self.fixture, name), timezone.now() + timedelta(hours=1))
            for name in ("admin", "worker", "outsider")
        }
        self.client = Client()

    def tearDown(self):
        transaction.set_rollback(True)
        self.atomic.__exit__(None, None, None)

    def get(self, path, role="admin"):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(path, HTTP_AUTHORIZATION="Bearer " + self.tokens[role])
        return response, len(captured)

    def project(self, number=0, **changes):
        return Project.objects.create(
            code=f"PERF-{number}",
            name=f"Proyecto {number}",
            area=self.fixture.area,
            type_code="GENERAL",
            start_date=self.fixture.today,
            end_date=None,
            available=True,
            worker_create=True,
            worker_edit=True,
            worker_state=True,
            **changes,
        )

    def task(self, project, number, **changes):
        return Task.objects.create(
            project=project,
            name=f"Tarea {number}",
            responsible=self.fixture.worker.worker,
            start_date=self.fixture.today,
            due_date=self.fixture.today + timedelta(days=10),
            state_code="PENDIENTE",
            **changes,
        )

    def test_project_query_count_is_constant_for_1_20_120(self):
        counts = {"admin": [], "worker": []}
        for size in (1, 20, 120):
            for number in range(Project.objects.count(), size):
                project = self.project(number)
                ProjectMember.objects.create(project=project, worker=self.fixture.worker.worker)
            for role in counts:
                response, count = self.get("/api/projects/", role)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["results"]), min(size, 25))
                self.assertEqual(response.json()["total"], size)
                self.assertTrue(all(p["permissions"]["create_tasks"] for p in response.json()["results"]))
                self.assertTrue(all(p["has_associated_workers"] is True for p in response.json()["results"]))
                self.assertTrue(all(p["areas"] == [{"id": self.fixture.area.pk, "name": self.fixture.area.name}] for p in response.json()["results"]))
                counts[role].append(count)
        for values in counts.values():
            self.assertEqual(len(set(values)), 1)
            self.assertLessEqual(max(values), 15)

    def test_associated_workers_uses_current_project_members_not_publication_or_state(self):
        project = self.project()
        path = f"/api/projects/{project.pk}/"
        self.assertIs(self.get("/api/projects/")[0].json()["results"][0]["has_associated_workers"], False)
        member = ProjectMember.objects.create(project=project, worker=self.fixture.worker.worker, active=False)
        self.assertIs(self.get("/api/projects/")[0].json()["results"][0]["has_associated_workers"], False)
        member.active = True
        member.save(update_fields=["active"])
        for available in (False, True):
            for code in ("PLANIFICADO", "EN_CURSO", "PAUSADO", "FINALIZADO", "CANCELADO"):
                Project.objects.filter(pk=project.pk).update(available=available, state_code=code)
                self.assertIs(self.get("/api/projects/")[0].json()["results"][0]["has_associated_workers"], True)
        self.assertIs(self.get(path)[0].json()["has_associated_workers"], True)
        member.delete()
        self.assertIs(self.get("/api/projects/")[0].json()["results"][0]["has_associated_workers"], False)
        for position, role in enumerate(("RESPONSABLE", "COLABORADOR", "REVISOR"), start=1):
            project = self.project(position)
            ProjectMember.objects.create(project=project, worker=self.fixture.admin.worker, project_role=role)
        self.assertEqual(sum(p["has_associated_workers"] for p in self.get("/api/projects/")[0].json()["results"]), 3)

    def test_associated_workers_preserves_visibility_archiving_and_creation_contract(self):
        response = self.fixture.request("post", data=self.fixture.project_payload(
            mode="assigned", worker_ids=[self.fixture.worker.worker_id], end_date=None,
        ), user=self.fixture.admin)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIs(response.json()["has_associated_workers"], True)
        project_id = response.json()["id"]
        worker = self.get("/api/projects/", "worker")[0].json()["results"]
        self.assertEqual([p["id"] for p in worker], [project_id])
        self.assertIs(worker[0]["has_associated_workers"], True)
        self.assertEqual(self.get("/api/projects/", "outsider")[0].json()["results"], [])
        ProjectMember.objects.filter(project_id=project_id).update(active=False)
        self.assertEqual(self.get("/api/projects/", "worker")[0].json()["results"], [])
        self.assertIs(self.get("/api/projects/")[0].json()["results"][0]["has_associated_workers"], False)
        Project.objects.filter(pk=project_id).update(archived=True)
        self.assertEqual(self.get("/api/projects/")[0].json()["results"], [])
        response = self.fixture.request("post", data=self.fixture.project_payload(end_date=None), user=self.fixture.admin)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIs(response.json()["has_associated_workers"], False)

    def test_task_query_count_is_constant_for_1_20_100(self):
        project = self.project()
        ProjectMember.objects.create(project=project, worker=self.fixture.worker.worker)
        counts = {"admin": [], "worker": []}
        for size in (1, 20, 100):
            for number in range(Task.objects.count(), size):
                task = self.task(project, number)
                TaskLabel.objects.create(task=task, name=f"Etiqueta {number}", kind="TECNICO")
            for role in counts:
                response, count = self.get(f"/api/projects/{project.pk}/tasks/", role)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()), size)
                self.assertTrue(all(len(t["labels"]) == 1 for t in response.json()))
                counts[role].append(count)
        for values in counts.values():
            self.assertEqual(len(set(values)), 1)
            self.assertLessEqual(max(values), 25)

    def test_account_and_candidate_counts_are_constant(self):
        counts = {"accounts": [], "workers": []}
        for size in (3, 60):
            for number in range(User.objects.count(), size):
                self.fixture.account(f"candidate{number}", "TRABAJADOR", self.fixture.area)
            for name, path in (
                ("accounts", "/api/auth/admin/accounts/"),
                ("workers", f"/api/projects/workers/?area_ids={self.fixture.area.pk}"),
            ):
                response, count = self.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["results"]) if name == "accounts" else len(response.json()), min(size, 25) if name == "accounts" else size - 2)
                if name == "accounts":
                    self.assertEqual(response.json()["total"], size)
                counts[name].append(count)
        for values in counts.values():
            self.assertEqual(len(set(values)), 1)
            self.assertLessEqual(max(values), 12)
        for role in ("worker", "outsider"):
            self.assertEqual(self.get("/api/auth/admin/accounts/", role)[0].status_code, 403)
            self.assertEqual(
                self.get(f"/api/projects/workers/?area_ids={self.fixture.area.pk}", role)[0].status_code, 403
            )

    def test_bulk_area_summary_preserves_multiple_inactive_and_all_areas(self):
        worker = self.fixture.worker.worker
        WorkerArea.objects.create(worker=worker, area=self.fixture.area)
        WorkerArea.objects.create(worker=worker, area=self.fixture.other_area)
        other = self.fixture.outsider.worker
        WorkerArea.objects.create(worker=other, area=self.fixture.inactive_area)
        admin = self.fixture.admin.worker
        admin.all_areas = True
        admin.save(update_fields=["all_areas"])
        workers = [worker, other, admin]
        summaries = area_summaries(workers)
        for entry in workers:
            self.assertEqual(summaries[entry.pk], area_summary(entry))
        self.assertEqual(len(summaries[worker.pk]["areas"]), 2)
        self.assertEqual(summaries[other.pk]["areas"], [])
        project = self.project()
        ProjectMember.objects.create(project=project, worker=worker)
        candidates = self.get(f"/api/projects/workers/?area_ids={self.fixture.area.pk}")[0].json()
        self.assertEqual(next(c["project_count"] for c in candidates if c["id"] == worker.pk), 1)

    def test_fresh_role_area_membership_and_flags_on_each_request(self):
        project = self.project()
        member = ProjectMember.objects.create(project=project, worker=self.fixture.worker.worker)
        self.task(project, 0)
        path = f"/api/projects/{project.pk}/tasks/"
        self.assertEqual(self.get(path, "outsider")[0].status_code, 404)
        self.assertTrue(self.get(path, "worker")[0].json()[0]["permissions"]["edit"])
        project.worker_edit = project.worker_state = project.worker_create = False
        project.save(update_fields=["worker_edit", "worker_state", "worker_create"])
        permissions = self.get(path, "worker")[0].json()[0]["permissions"]
        self.assertFalse(any(permissions.values()))
        member.active = False
        member.save(update_fields=["active"])
        self.assertEqual(self.get(path, "worker")[0].status_code, 404)
        member.active = True
        member.save(update_fields=["active"])
        WorkerArea.objects.create(worker=self.fixture.worker.worker, area=self.fixture.other_area)
        self.assertEqual(self.get(path, "worker")[0].status_code, 404)
        UserRole.objects.filter(user=self.fixture.admin).update(role_code="TRABAJADOR")
        self.assertEqual(self.get("/api/auth/admin/accounts/")[0].status_code, 403)
        self.assertEqual(self.get(path)[0].status_code, 404)
        UserRole.objects.filter(user=self.fixture.worker).delete()
        self.assertEqual(self.get(path, "worker")[0].status_code, 401)

    def test_task_relationships_supervision_and_configured_states(self):
        project = self.project()
        member = ProjectMember.objects.create(
            project=project, worker=self.fixture.worker.worker, project_role="RESPONSABLE"
        )
        first = self.task(project, 0)
        child = self.task(project, 1, parent=first)
        hidden = self.task(project, 2)
        hidden.responsible = self.fixture.outsider.worker
        hidden.save(update_fields=["responsible"])
        TaskDependency.objects.create(project=project, predecessor=first, successor=child)
        TaskDependency.objects.create(project=project, predecessor=hidden, successor=child)
        for position, code in enumerate(("PENDIENTE", "EN_CURSO", "COMPLETADA", "CANCELADA")):
            ProjectTaskStatus.objects.create(project=project, code=code, name=f"Configurado {code}", position=position)
        path = f"/api/projects/{project.pk}/tasks/"
        admin = self.get(path)[0].json()
        worker = self.get(path, "worker")[0].json()
        self.assertEqual(len(admin), 3)
        self.assertEqual(len(worker), 2)
        self.assertEqual(admin[1]["dependencies"], [first.pk, hidden.pk])
        self.assertEqual(worker[1]["dependencies"], [first.pk])
        self.assertEqual(worker[0]["state_name"], "Configurado PENDIENTE")
        self.assertIn({"target": "CANCELADA", "reason_required": False}, worker[0]["transitions"])
        member.project_role = "PARTICIPANTE"
        member.save(update_fields=["project_role"])
        self.assertIn(
            {"target": "CANCELADA", "reason_required": False}, self.get(path, "worker")[0].json()[0]["transitions"]
        )
        project.state_code = "FINALIZADO"
        project.save(update_fields=["state_code"])
        self.assertFalse(any(self.get(path)[0].json()[0]["permissions"].values()))
        self.assertEqual(self.get(path)[0].json()[0]["transitions"], [])

    def test_refresh_rotation_expiration_revocation_and_csrf(self):
        user = self.fixture.worker
        unmatched = issue_stored(user, "refresh", timezone.now() + timedelta(hours=1))
        with self.assertRaises(InvalidToken):
            consume_stored(unmatched, "refresh", user=self.fixture.admin)
        self.assertIsNone(AuthToken.objects.get(digest=digest_token(unmatched)).consumed_at)
        refresh = issue_stored(user, "refresh", timezone.now() + timedelta(hours=1))
        client = Client(enforce_csrf_checks=True)
        client.cookies[settings.AUTH_REFRESH_COOKIE] = refresh
        self.assertEqual(client.post("/api/auth/refresh/").status_code, 403)
        csrf = client.get("/api/auth/csrf/").json()["csrf_token"]
        renewed = client.post("/api/auth/refresh/", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(renewed.status_code, 200)
        new_access = renewed.json()["access"]
        self.assertEqual(client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {new_access}").status_code, 200)
        replay = Client(enforce_csrf_checks=True)
        replay.cookies[settings.AUTH_REFRESH_COOKIE] = refresh
        csrf2 = replay.get("/api/auth/csrf/").json()["csrf_token"]
        self.assertEqual(replay.post("/api/auth/refresh/", HTTP_X_CSRFTOKEN=csrf2).status_code, 401)
        expired = issue_stored(user, "access", timezone.now() - timedelta(seconds=1))
        self.assertEqual(client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {expired}").status_code, 401)
        AuthToken.objects.filter(user=user, purpose="access").update(consumed_at=timezone.now())
        self.assertEqual(client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {new_access}").status_code, 401)
        UserRole.objects.filter(user=user).delete()
        self.assertEqual(client.post("/api/auth/refresh/", HTTP_X_CSRFTOKEN=csrf).status_code, 401)

    def test_project_creation_and_login_contract(self):
        user = self.fixture.admin
        user.set_password("Synthetic-local-password-93!")
        user.save(update_fields=["password"])
        client = Client(enforce_csrf_checks=True)
        csrf = client.get("/api/auth/csrf/").json()["csrf_token"]
        logged = client.post(
            "/api/auth/login/",
            data=json.dumps({"email": user.email, "password": "Synthetic-local-password-93!"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(logged.status_code, 200)
        response = client.post(
            "/api/projects/",
            data=json.dumps(
                {
                    "name": "Proyecto único",
                    "area_ids": [self.fixture.area.pk],
                    "end_date": None,
                    "mode": "assigned",
                    "worker_ids": [self.fixture.worker.worker_id],
                    "worker_contribute": True,
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + logged.json()["access"],
        )
        self.assertEqual(response.status_code, 201, response.content)
        project = Project.objects.get(pk=response.json()["id"])
        self.assertEqual(project.area_id, self.fixture.area.pk)
        self.assertIsNone(project.end_date)
        self.assertEqual(ProjectMember.objects.filter(project=project, active=True).count(), 1)
