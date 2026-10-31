import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

from django.db import IntegrityError, connection
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.utils import timezone

from apps.teams.models import Team, TeamMember
from apps.authentication.models import AuthToken, Role, User, UserRole
from apps.authentication.tokens import issue_access
from apps.organization.models import Area, Specialty
from apps.projects.models import (
    Project,
    ProjectFavorite,
    ProjectMember,
    ProjectRequirement,
    ProjectState,
    ProjectTaskStatus,
    ProjectType,
    Task,
    TaskAssignee,
    TaskDependency,
    TaskLabel,
    TaskState,
    TaskStateDefinition,
    TaskStatusTemplate,
    TaskTransition,
    TaskActivity,
    TaskRelation,
    TaskCalendarIntent,
    ProjectTeam, TaskOrder,
)
from apps.projects.serializers import ProjectCreateSerializer
from apps.projects.services.projects import create_project
from apps.projects.services.tasks import dependency_would_cycle
from apps.workers.models import Worker, WorkerArea


class ProjectFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("AUTH_TEST_SQLITE") != "1" or connection.vendor != "sqlite":
            raise unittest.SkipTest("Project tests require the existing explicit isolated SQLite mode")
        cls.tables = [
            Area,
            Specialty,
            Worker,
            WorkerArea,
            Team,
            TeamMember,
            User,
            Role,
            UserRole,
            AuthToken,
            ProjectType,
            ProjectState,
            TaskState,
            TaskTransition,
            Project,
            ProjectFavorite,
            ProjectMember,
            ProjectRequirement,
            ProjectTaskStatus,
            TaskStateDefinition,
            TaskStatusTemplate,
            Task,
            TaskAssignee,
            TaskDependency,
            TaskLabel,
            TaskRelation,
            TaskActivity,
            ProjectTeam, TaskOrder, TaskCalendarIntent,
        ]
        with connection.schema_editor() as editor:
            for model in cls.tables:
                editor.create_model(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(cls.tables):
                editor.delete_model(model)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.area = Area.objects.create(code="TEST_A", name="Área sintética A", active=True)
        self.other_area = Area.objects.create(code="TEST_B", name="Área sintética B", active=True)
        self.inactive_area = Area.objects.create(code="TEST_OFF", name="Área sintética inactiva", active=False)
        for code, name in (("ADMINISTRADOR", "Administrador"), ("TRABAJADOR", "Trabajador")):
            Role.objects.create(code=code, name=name)
        ProjectType.objects.create(code="GENERAL", name="General")
        ProjectState.objects.create(code="PLANIFICADO", name="Planificado")
        for code in ("PENDIENTE", "EN_CURSO", "EN_REVISION", "OBSERVADA", "COMPLETADA", "BLOQUEADA", "CANCELADA"):
            TaskState.objects.create(code=code, name=code, terminal=code in ("COMPLETADA", "CANCELADA"))
        for source, target, supervision, reason in (
            ("PENDIENTE", "EN_CURSO", False, False),
            ("PENDIENTE", "BLOQUEADA", False, True),
            ("PENDIENTE", "CANCELADA", True, True),
            ("EN_CURSO", "EN_REVISION", False, False),
            ("EN_REVISION", "COMPLETADA", True, False),
            ("COMPLETADA", "EN_CURSO", True, True),
        ):
            TaskTransition.objects.create(
                source=source, target=target, supervision_required=supervision, reason_required=reason
            )
        self.admin = self.account("admin", "ADMINISTRADOR", self.other_area)
        self.worker = self.account("worker", "TRABAJADOR", self.area)
        self.outsider = self.account("outsider", "TRABAJADOR", self.other_area)
        self.tokens = {}
        self.today = timezone.localdate()

    def tearDown(self):
        with connection.cursor() as cursor:
            for model in reversed(self.tables):
                cursor.execute(f'DELETE FROM "{model._meta.db_table}"')

    def account(self, code, role, area, active=True):
        worker = Worker.objects.create(
            code=code,
            area=area,
            first_names="Nombre sintético",
            last_names=code,
            email=f"{code}@example.invalid",
            active=True,
        )
        user = User.objects.create(worker=worker, username=f"test_{code}", password="!", is_active=active)
        UserRole.objects.create(user=user, role_code=role)
        return user

    def request(self, method, path="", data=None, user=None):
        headers = {}
        if user:
            if user.pk not in self.tokens:
                self.tokens[user.pk] = issue_access(user, timezone.now() + timedelta(hours=1))
            headers["HTTP_AUTHORIZATION"] = f"Bearer {self.tokens[user.pk]}"
        kwargs = {"content_type": "application/json", **headers}
        if data is not None:
            kwargs["data"] = json.dumps(data)
        return getattr(self.client, method)(f"/api/projects/{path}", **kwargs)

    def project_payload(self, **changes):
        return {
            "name": "Proyecto sintético",
            "area_ids": [self.area.pk],
            "mode": "available",
            "end_date": str(self.today + timedelta(days=30)),
            **changes,
        }

    def project(self, **changes):
        requirements = changes.pop("requirements", [])
        legacy_permissions = {
            field: changes.pop(field) for field in ("worker_edit", "worker_state") if field in changes
        }
        response = self.request("post", data=self.project_payload(**changes), user=self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        project = Project.objects.get(pk=response.json()["id"])
        for field, value in legacy_permissions.items():
            setattr(project, field, value)
        if legacy_permissions:
            project.save(update_fields=list(legacy_permissions))
        ProjectRequirement.objects.bulk_create([ProjectRequirement(project=project, **label) for label in requirements])
        return project

    def assigned_project(self, **changes):
        return self.project(
            mode="assigned",
            worker_ids=[self.worker.worker_id],
            end_date=str(self.today + timedelta(days=30)),
            **changes,
        )

    def task_payload(self, **changes):
        return {
            "name": "Tarea sintética",
            "state_code": "PENDIENTE",
            "priority": 3,
            "due_date": str(self.today + timedelta(days=10)),
            **changes,
        }

    def task(self, project, **changes):
        response = self.request("post", f"{project.pk}/tasks/", self.task_payload(**changes), self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        return Task.objects.get(pk=response.json()["id"])

    def test_all_endpoints_require_existing_authentication(self):
        project = self.project()
        task = self.task(project)
        for method, path, data in (
            ("get", "", None),
            ("get", "catalogs/", None),
            ("get", "workers/", None),
            ("post", "", self.project_payload()),
            ("get", f"{project.pk}/", None),
            ("get", f"{project.pk}/tasks/", None),
            ("post", f"{project.pk}/tasks/", self.task_payload()),
            ("patch", f"{project.pk}/tasks/{task.pk}/", {"name": "Cambio"}),
            ("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "EN_CURSO"}),
            ("post", f"{project.pk}/tasks/{task.pk}/dependencies/", {"predecessor_id": task.pk}),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, data).status_code, 401)

    def test_available_creation_persists_areas_defaults_and_server_start_without_requirements(self):
        project = self.project()
        self.assertNotIn("proyecto_area", connection.introspection.table_names())
        self.assertEqual(project.area_id, self.area.pk)
        self.assertEqual(project.start_date, self.today)
        self.assertEqual(project.end_date, self.today + timedelta(days=30))
        self.assertFalse(project.worker_edit)
        self.assertFalse(project.worker_state)
        self.assertFalse(project.worker_create)
        self.assertTrue(project.available)
        self.assertFalse(ProjectMember.objects.filter(project=project).exists())
        self.assertFalse(ProjectRequirement.objects.filter(project=project).exists())

    def test_assigned_creation_is_atomic_and_uses_project_membership_not_global_roles(self):
        project = self.assigned_project(worker_contribute=True)
        self.assertFalse(project.available)
        self.assertFalse(project.worker_edit)
        self.assertTrue(project.worker_create)
        self.assertTrue(project.worker_state)
        self.assertEqual(ProjectMember.objects.get(project=project).worker_id, self.worker.worker_id)
        self.assertEqual(self.worker.global_role, "TRABAJADOR")

    def test_creation_rolls_back_all_records_on_membership_failure(self):
        with patch(
            "apps.projects.services.projects.ProjectMember.objects.bulk_create",
            side_effect=IntegrityError("synthetic failure"),
        ):
            response = self.request(
                "post",
                data=self.project_payload(
                    mode="assigned",
                    worker_ids=[self.worker.worker_id],
                    end_date=str(self.today + timedelta(days=30)),
                ),
                user=self.admin,
            )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Project.objects.exists())
        self.assertFalse(ProjectMember.objects.exists())

    def test_bad_project_payloads_never_create_partial_records(self):
        for changes in (
            {"name": " "},
            {"area_ids": []},
            {"area_ids": [self.inactive_area.pk]},
            {"area_ids": [self.area.pk, self.area.pk]},
            {"area_ids": [999999]},
            {"mode": "assigned", "worker_ids": []},
            {"mode": "available", "worker_ids": [self.worker.worker_id]},
            {
                "mode": "assigned",
                "worker_ids": [self.outsider.worker_id],
                "end_date": str(self.today + timedelta(days=10)),
            },
            {"end_date": str(self.today - timedelta(days=1))},
            {"start_date": str(self.today)},
            {"requirements": [{"name": "Igual", "kind": "technical"}, {"name": "igual", "kind": "technical"}]},
            {"global_role": "ADMINISTRADOR"},
        ):
            with self.subTest(changes=changes):
                response = self.request("post", data=self.project_payload(**changes), user=self.admin)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(Project.objects.exists())

    def test_only_admin_can_create_projects_and_fetch_worker_candidates(self):
        self.assertEqual(
            self.request(
                "post", data=self.project_payload(worker_edit=True, worker_state=True), user=self.worker
            ).status_code,
            403,
        )
        self.assertEqual(self.request("get", f"workers/?area_ids={self.area.pk}", user=self.worker).status_code, 403)
        self.assertEqual(self.request("get", "catalogs/", user=self.worker).json()["areas"], [])
        self.assertEqual(
            {area["id"] for area in self.request("get", "catalogs/", user=self.admin).json()["areas"]},
            {self.area.pk, self.other_area.pk},
        )

    def test_end_date_is_optional_for_both_modes_and_today_is_allowed(self):
        for mode in ("available", "assigned"):
            for omit_date in (False, True):
                with self.subTest(mode=mode, omit_date=omit_date):
                    payload = self.project_payload(
                        mode=mode,
                        end_date=None,
                        worker_ids=[self.worker.worker_id] if mode == "assigned" else [],
                    )
                    if omit_date:
                        del payload["end_date"]
                    response = self.request("post", data=payload, user=self.admin)
                    self.assertEqual(response.status_code, 201, response.content)
                    self.assertIsNone(response.json()["end_date"])
                    project = Project.objects.get(pk=response.json()["id"])
                    self.assertIsNone(project.end_date)
                    self.assertEqual(project.start_date, self.today)
                    self.assertEqual(project.area_id, self.area.pk)
                    self.assertEqual(response.json()["areas"], [{"id": self.area.pk, "name": self.area.name}])
                    self.assertEqual(ProjectMember.objects.filter(project=project).count(), int(mode == "assigned"))
        project = self.project(end_date=str(self.today))
        self.assertEqual(project.start_date, project.end_date)
        self.assertEqual(self.request("get", "catalogs/", user=self.admin).json()["creation_date"], str(self.today))

    def test_creation_requires_exactly_one_area_in_each_mode(self):
        for mode in ("available", "assigned"):
            for areas in (None, [], [self.area.pk, self.other_area.pk], [self.area.pk, self.area.pk]):
                with self.subTest(mode=mode, areas=areas):
                    payload = self.project_payload(
                        mode=mode,
                        end_date=None,
                        worker_ids=[self.worker.worker_id] if mode == "assigned" else [],
                    )
                    if areas is None:
                        del payload["area_ids"]
                    else:
                        payload["area_ids"] = areas
                    response = self.request("post", data=payload, user=self.admin)
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertIn("area_ids", response.json())
                    self.assertFalse(Project.objects.exists())
                    self.assertFalse(ProjectMember.objects.exists())

    def test_creation_service_rejects_missing_or_multiple_areas(self):
        from rest_framework.exceptions import ValidationError

        for areas in (None, [], [self.area.pk, self.other_area.pk]):
            with self.subTest(areas=areas):
                data = self.project_payload(area_ids=areas)
                if areas is None:
                    del data["area_ids"]
                with self.assertRaises(ValidationError) as error:
                    create_project(self.admin, data)
                self.assertIn("area_ids", error.exception.detail)
                self.assertFalse(Project.objects.exists())

    def test_optional_end_date_rejects_empty_malformed_or_past_dates(self):
        for date in ("", "not-a-date", "2030-02-30", str(self.today - timedelta(days=1))):
            with self.subTest(date=date):
                response = self.request("post", data=self.project_payload(end_date=date), user=self.admin)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("end_date", response.json())
                self.assertFalse(Project.objects.exists())

    def test_single_area_candidates_and_assignment_preserve_worker_multiple_areas(self):
        WorkerArea.objects.create(worker=self.outsider.worker, area=self.area)
        WorkerArea.objects.create(worker=self.outsider.worker, area=self.other_area)
        for area in (self.area, self.other_area):
            candidates = self.request("get", f"workers/?area_ids={area.pk}", user=self.admin)
            self.assertEqual(candidates.status_code, 200)
            self.assertIn(self.outsider.worker_id, {item["id"] for item in candidates.json()})
            project = self.project(mode="assigned", area_ids=[area.pk], worker_ids=[self.outsider.worker_id])
            self.assertEqual(project.area_id, area.pk)
            self.assertEqual(self.request("get", f"{project.pk}/", user=self.outsider).status_code, 200)
        self.assertEqual(WorkerArea.objects.filter(worker=self.outsider.worker).count(), 2)

    def test_worker_candidates_reject_missing_and_multiple_project_areas(self):
        for query in ("", f"?area_ids={self.area.pk},{self.other_area.pk}"):
            response = self.request("get", f"workers/{query}", user=self.admin)
            self.assertEqual(response.status_code, 400)
            self.assertIn("area_ids", response.json())

    def test_project_area_is_the_only_source_for_visibility_and_details(self):
        project = self.project()
        details = self.request("get", f"{project.pk}/", user=self.admin)
        self.assertEqual(details.json()["areas"], [{"id": self.area.pk, "name": self.area.name}])
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.outsider).status_code, 404)
        Project.objects.filter(pk=project.pk).update(area=self.other_area)
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.worker).status_code, 404)
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.outsider).status_code, 200)

    def test_tasks_without_project_end_keep_supplied_date_and_parent_bounds(self):
        project = self.project(
            mode="assigned",
            worker_ids=[self.worker.worker_id],
            worker_contribute=True,
            end_date=None,
        )
        far_date = self.today + timedelta(days=365)
        response = self.request("post", f"{project.pk}/tasks/", self.task_payload(due_date=str(far_date)), self.worker)
        self.assertEqual(response.status_code, 201, response.content)
        parent_id = response.json()["id"]
        self.assertEqual(response.json()["responsible_id"], self.worker.worker_id)
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/", self.task_payload(parent_id=parent_id), self.worker
            ).status_code,
            201,
        )
        for due_date, parent in (("", None), (str(self.today - timedelta(days=1)), None),
                                 (str(far_date + timedelta(days=1)), parent_id)):
            with self.subTest(due_date=due_date, parent=parent):
                invalid = self.request(
                    "post",
                    f"{project.pk}/tasks/",
                    self.task_payload(due_date=due_date, parent_id=parent),
                    self.admin,
                )
                self.assertEqual(invalid.status_code, 400, invalid.content)
                self.assertIn("due_date", invalid.json())
        edit = self.request(
            "patch",
            f"{project.pk}/tasks/{parent_id}/",
            {
                "due_date": str(far_date + timedelta(days=10)),
                "expected_calendar_revision": self.request("get", f"{project.pk}/tasks/{parent_id}/", user=self.admin).json()["task"]["calendar_revision"],
                "calendar_intent_id": str(uuid4()),
            },
            self.admin,
        )
        self.assertEqual(edit.status_code, 200, edit.content)

    def test_project_form_validation_returns_field_errors_without_partial_records(self):
        response = self.request(
            "post",
            data={"name": " ", "area_ids": [], "mode": "available", "end_date": "invalid"},
            user=self.admin,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {"name", "area_ids", "end_date"})
        self.assertFalse(Project.objects.exists())
        self.assertFalse(ProjectMember.objects.exists())

    def test_task_form_validation_returns_field_errors_without_creating_tasks(self):
        project = self.assigned_project()
        response = self.request(
            "post",
            f"{project.pk}/tasks/",
            self.task_payload(name=" ", description="x" * 10001, due_date="invalid", priority=5),
            self.admin,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {"name", "description", "due_date", "priority"})
        self.assertFalse(Task.objects.exists())
        self.assertFalse(TaskLabel.objects.exists())

    def test_server_start_date_uses_lima_when_utc_is_already_next_day(self):
        instant = datetime(2030, 1, 2, 3, 30, tzinfo=UTC)
        with timezone.override("America/Lima"), patch("django.utils.timezone.now", return_value=instant):
            serializer = ProjectCreateSerializer(data=self.project_payload(end_date="2030-01-01"))
            self.assertTrue(serializer.is_valid(), serializer.errors)
            project = create_project(self.admin, serializer.validated_data)
        self.assertEqual(str(project.start_date), "2030-01-01")
        self.assertEqual(project.start_date, project.end_date)

    def test_date_is_checked_again_at_creation_after_validation(self):
        serializer = ProjectCreateSerializer(data=self.project_payload(end_date=str(self.today)))
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with patch("apps.projects.services.projects.timezone.localdate", return_value=self.today + timedelta(days=1)):
            from rest_framework.exceptions import ValidationError

            with self.assertRaises(ValidationError):
                create_project(self.admin, serializer.validated_data)
        self.assertFalse(Project.objects.exists())

    def test_edition_allows_own_creation_and_state_but_not_other_field_edits(self):
        project = self.assigned_project(worker_contribute=True)
        self.assertTrue(self.request("get", f"{project.pk}/", user=self.worker).json()["permissions"]["create_tasks"])
        response = self.request(
            "post",
            f"{project.pk}/tasks/",
            self.task_payload(labels=[{"name": "Propia", "kind": "technical"}]),
            self.worker,
        )
        self.assertEqual(response.status_code, 201, response.content)
        task = Task.objects.get(pk=response.json()["id"])
        self.assertEqual(task.responsible_id, self.worker.worker_id)
        self.assertEqual(TaskLabel.objects.get(task=task).name, "Propia")
        self.assertFalse(ProjectRequirement.objects.filter(project=project).exists())
        self.assertFalse(response.json()["permissions"]["edit"])
        self.assertFalse(response.json()["permissions"]["assign"])
        self.assertEqual(
            self.request(
                "patch", f"{project.pk}/tasks/{task.pk}/", {"name": "Prohibido", "priority": 1}, self.worker
            ).status_code,
            403,
        )
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            200,
        )

    def test_published_edition_does_not_grant_creation_or_state_without_membership(self):
        project = self.project(worker_contribute=True)
        details = self.request("get", f"{project.pk}/", user=self.worker)
        self.assertEqual(details.status_code, 200)
        self.assertFalse(details.json()["permissions"]["create_tasks"])
        self.assertEqual(
            self.request("post", f"{project.pk}/tasks/", self.task_payload(), self.worker).status_code, 403
        )
        ProjectMember.objects.create(project=project, worker=self.worker.worker)
        task = self.task(project, responsible_id=self.worker.worker_id)
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).delete()
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            403,
        )

    def test_old_edit_and_state_flags_never_implicitly_grant_task_creation(self):
        project = self.assigned_project(worker_edit=True, worker_state=True)
        self.assertFalse(project.worker_create)
        self.assertFalse(self.request("get", f"{project.pk}/", user=self.worker).json()["permissions"]["create_tasks"])
        self.assertEqual(
            self.request("post", f"{project.pk}/tasks/", self.task_payload(), self.worker).status_code, 403
        )

    def test_worker_cannot_create_for_others_or_bypass_state_transitions(self):
        colleague = self.account("colleague", "TRABAJADOR", self.area)
        project = self.project(
            mode="assigned", worker_ids=[self.worker.worker_id, colleague.worker_id], worker_contribute=True
        )
        for changes, status in (
            ({"responsible_id": colleague.worker_id}, 403),
            ({"state_code": "COMPLETADA"}, 400),
            ({"state_code": "EN_CURSO"}, 400),
            ({"state_code": "CANCELADA", "reason": "No evita supervisión"}, 400),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.request("post", f"{project.pk}/tasks/", self.task_payload(**changes), self.worker).status_code,
                    status,
                )
        self.assertFalse(Task.objects.exists())
        foreign_task = self.task(project, responsible_id=colleague.worker_id)
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{foreign_task.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            404,
        )
        self.assertEqual(
            self.request(
                "post",
                f"{project.pk}/tasks/{foreign_task.pk}/dependencies/",
                {"predecessor_id": foreign_task.pk},
                self.worker,
            ).status_code,
            403,
        )

    def test_edition_subtasks_require_own_parent_and_keep_automatic_self_assignment(self):
        project = self.assigned_project(worker_contribute=True)
        parent = self.task(project, responsible_id=self.worker.worker_id)
        response = self.request("post", f"{project.pk}/tasks/", self.task_payload(parent_id=parent.pk), self.worker)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["responsible_id"], self.worker.worker_id)
        self.assertEqual(response.json()["parent_id"], parent.pk)
        self.assertTrue(response.json()["permissions"]["create_child"])
        unassigned = self.task(project)
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/", self.task_payload(parent_id=unassigned.pk), self.worker
            ).status_code,
            403,
        )

    def test_edition_creation_still_requires_authorized_area_active_membership_and_open_project(self):
        project = self.assigned_project(worker_contribute=True)
        ProjectMember.objects.create(project=project, worker=self.outsider.worker)
        self.assertEqual(
            self.request("post", f"{project.pk}/tasks/", self.task_payload(), self.outsider).status_code, 404
        )
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=False)
        self.assertEqual(
            self.request("post", f"{project.pk}/tasks/", self.task_payload(), self.worker).status_code, 404
        )
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=True)
        project.state_code = "FINALIZADO"
        project.save()
        self.assertFalse(self.request("get", f"{project.pk}/", user=self.worker).json()["permissions"]["create_tasks"])
        self.assertEqual(
            self.request("post", f"{project.pk}/tasks/", self.task_payload(), self.worker).status_code, 400
        )
        self.assertFalse(Task.objects.exists())

    def test_available_project_visibility_requires_authorized_area_and_honors_revocation(self):
        project = self.project()
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.worker).status_code, 200)
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.outsider).status_code, 404)
        ProjectMember.objects.create(project=project, worker=self.worker.worker, active=False)
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.worker).status_code, 404)
        self.assertEqual(self.request("get", user=self.outsider).json()["results"], [])

    def test_assigned_and_confidential_projects_require_participation_and_area(self):
        assigned = self.assigned_project()
        extra = self.account("same_area", "TRABAJADOR", self.area)
        self.assertEqual(self.request("get", f"{assigned.pk}/", user=extra).status_code, 404)
        public = self.project()
        public.confidential = True
        public.save()
        self.assertEqual(self.request("get", f"{public.pk}/", user=self.worker).status_code, 404)
        ProjectMember.objects.create(project=public, worker=self.worker.worker)
        self.assertEqual(self.request("get", f"{public.pk}/", user=self.worker).status_code, 200)
        self.worker.worker.area = self.other_area
        self.worker.worker.save()
        self.assertEqual(self.request("get", f"{assigned.pk}/", user=self.worker).status_code, 404)

    def test_all_areas_and_multiple_areas_use_existing_backend_resolution(self):
        self.worker.worker.all_areas = True
        self.worker.worker.area = None
        self.worker.worker.save()
        new_area = Area.objects.create(code="TEST_NEW", name="Área futura sintética", active=True)
        project = self.project(area_ids=[new_area.pk])
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.worker).status_code, 200)
        WorkerArea.objects.create(worker=self.outsider.worker, area=self.area)
        candidates = self.request("get", f"workers/?area_ids={self.area.pk}&q=Nombre%20outsider", user=self.admin)
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual([item["id"] for item in candidates.json()], [self.outsider.worker_id])
        self.assertEqual(set(candidates.json()[0]), {"id", "name", "project_count"})

    def test_worker_search_counts_memberships_and_excludes_inactive_accounts(self):
        self.assigned_project()
        inactive = self.account("inactive", "TRABAJADOR", self.area, active=False)
        response = self.request("get", f"workers/?area_ids={self.area.pk}", user=self.admin)
        self.assertEqual(response.status_code, 200)
        result = {item["id"]: item for item in response.json()}
        self.assertEqual(result[self.worker.worker_id]["project_count"], 1)
        self.assertNotIn(inactive.worker_id, result)
        self.assertEqual(self.request("get", "workers/?area_ids=invalid", user=self.admin).status_code, 400)

    def test_admin_can_create_unassigned_tasks_without_a_phase_or_project_membership(self):
        project = self.project()
        task = self.task(project, labels=[{"name": "Propia", "kind": "nontechnical"}])
        self.assertIsNone(task.phase_id)
        self.assertIsNone(task.responsible_id)
        self.assertIsNotNone(task.created_at)
        self.assertEqual(TaskLabel.objects.get(task=task).name, "Propia")
        self.assertFalse(ProjectMember.objects.filter(project=project, worker=self.admin.worker).exists())

    def test_task_creation_validates_catalog_responsible_dates_and_foreign_requirements(self):
        project = self.project()
        other = self.project(requirements=[{"name": "Ajeno", "kind": "technical"}])
        requirement = ProjectRequirement.objects.get(project=other)
        for changes in (
            {"state_code": "INVALID"},
            {"priority": 5},
            {"due_date": str(self.today - timedelta(days=1))},
            {"responsible_id": self.worker.worker_id},
            {"requirement_ids": [requirement.pk]},
            {"created_at": "2020-01-01"},
            {"parent_id": 999999},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.request("post", f"{project.pk}/tasks/", self.task_payload(**changes), self.admin).status_code,
                    400,
                )
                self.assertFalse(Task.objects.filter(project=project).exists())

    def test_admin_initial_enabled_states_do_not_require_responsible(self):
        project = self.project()
        for code in ("EN_CURSO", "BLOQUEADA"):
            with self.subTest(state=code):
                response = self.request("post", f"{project.pk}/tasks/", self.task_payload(state_code=code), self.admin)
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(response.json()["state_code"], code)
                self.assertIsNone(response.json()["responsible_id"])
                self.assertEqual(response.json()["calendar_revision"], "1")

    def test_subtask_dates_and_parent_project_are_validated(self):
        project = self.assigned_project()
        parent = self.task(project, responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk, responsible_id=self.worker.worker_id)
        self.assertEqual(child.parent_id, parent.pk)
        self.assertIsNone(child.phase_id)
        other = self.project()
        self.assertEqual(
            self.request("post", f"{other.pk}/tasks/", self.task_payload(parent_id=parent.pk), self.admin).status_code,
            400,
        )
        self.assertEqual(
            self.request(
                "post",
                f"{project.pk}/tasks/",
                self.task_payload(parent_id=parent.pk, due_date=str(self.today + timedelta(days=11))),
                self.admin,
            ).status_code,
            400,
        )
        self.assertEqual(
            self.request(
                "patch",
                f"{project.pk}/tasks/{parent.pk}/",
                {"due_date": str(self.today + timedelta(days=5))},
                self.admin,
            ).status_code,
            400,
        )

    def test_default_worker_flags_deny_direct_edit_and_state_for_tasks_and_subtasks(self):
        project = self.assigned_project()
        parent = self.task(project, responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk, responsible_id=self.worker.worker_id)
        for task in (parent, child):
            self.assertEqual(
                self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"name": "Prohibido"}, self.worker).status_code,
                403,
            )
            self.assertEqual(
                self.request(
                    "post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
                ).status_code,
                403,
            )
        self.assertEqual(
            self.request("post", f"{project.pk}/tasks/", self.task_payload(), self.worker).status_code, 403
        )
        self.assertEqual(self.request("delete", f"{project.pk}/tasks/{parent.pk}/", user=self.worker).status_code, 405)

    def test_worker_edit_flag_is_independent_from_state_flag_and_cannot_change_responsible(self):
        project = self.assigned_project(worker_edit=True, requirements=[{"name": "Base", "kind": "technical"}])
        task = self.task(project, responsible_id=self.worker.worker_id)
        requirement = ProjectRequirement.objects.get(project=project)
        response = self.request(
            "patch",
            f"{project.pk}/tasks/{task.pk}/",
            {
                "name": "Editada",
                "priority": 2,
                "due_date": str(self.today + timedelta(days=9)),
                "expected_calendar_revision": self.request("get", f"{project.pk}/tasks/{task.pk}/", user=self.worker).json()["task"]["calendar_revision"],
                "calendar_intent_id": str(uuid4()),
                "requirement_ids": [requirement.pk],
                "labels": [{"name": "Extra", "kind": "nontechnical"}],
            },
            self.worker,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["labels"]), 2)
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            403,
        )
        self.assertEqual(
            self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"responsible_id": None}, self.worker).status_code,
            403,
        )
        self.assertEqual(
            self.request(
                "patch", f"{project.pk}/tasks/{task.pk}/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            400,
        )

    def test_worker_state_flag_is_independent_from_edit_flag(self):
        project = self.assigned_project(worker_state=True)
        task = self.task(project, responsible_id=self.worker.worker_id)
        response = self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "EN_CURSO"}, self.worker)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["state_code"], "EN_CURSO")
        self.assertEqual(
            self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"name": "Prohibido"}, self.worker).status_code, 403
        )

    def test_worker_flags_apply_to_assigned_subtasks(self):
        project = self.assigned_project(worker_edit=True, worker_state=True)
        parent = self.task(project)
        child = self.task(project, parent_id=parent.pk, responsible_id=self.worker.worker_id)
        response = self.request("get", f"{project.pk}/tasks/", user=self.worker)
        self.assertEqual([item["id"] for item in response.json()], [child.pk])
        self.assertEqual(
            self.request(
                "patch", f"{project.pk}/tasks/{child.pk}/", {"name": "Subtarea editada"}, self.worker
            ).status_code,
            200,
        )
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{child.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            200,
        )
        self.assertEqual(
            self.request("patch", f"{project.pk}/tasks/{parent.pk}/", {"name": "Padre ajeno"}, self.worker).status_code,
            404,
        )

    def test_worker_visibility_and_assignment_cannot_be_overridden_by_filters(self):
        project = self.assigned_project(worker_edit=True, worker_state=True)
        own = self.task(project, responsible_id=self.worker.worker_id)
        unassigned = self.task(project)
        self.assertEqual(
            [
                item["id"]
                for item in self.request(
                    "get", f"{project.pk}/tasks/?responsible_id=all&role=ADMINISTRADOR", user=self.worker
                ).json()
            ],
            [own.pk],
        )
        self.assertEqual(
            self.request("patch", f"{project.pk}/tasks/{unassigned.pk}/", {"name": "Intento"}, self.worker).status_code,
            404,
        )
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=False)
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{own.pk}/state/", {"state_code": "EN_CURSO"}, self.worker
            ).status_code,
            404,
        )

    def test_worker_state_permission_and_assignment_apply_without_supervision_requirement(self):
        project = self.assigned_project(worker_state=True)
        task = self.task(project, state_code="EN_REVISION", responsible_id=self.worker.worker_id)
        membership = ProjectMember.objects.get(project=project, worker=self.worker.worker)
        self.assertEqual(membership.project_role, "COLABORADOR")
        allowed = self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "COMPLETADA"}, self.worker)
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertEqual(TaskActivity.objects.get(task=task, field="state_code").actor_id, self.worker.pk)
        unassigned = self.task(project)
        count = TaskActivity.objects.count()
        denied = self.request("post", f"{project.pk}/tasks/{unassigned.pk}/state/", {"state_code": "COMPLETADA"}, self.worker)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(TaskActivity.objects.count(), count)
        project.worker_state = False
        project.save(update_fields=["worker_state"])
        denied = self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "PENDIENTE"}, self.worker)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(TaskActivity.objects.count(), count)
        allowed = self.request("post", f"{project.pk}/tasks/{unassigned.pk}/state/", {"state_code": "COMPLETADA"}, self.admin)
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.assertFalse(ProjectMember.objects.filter(project=project, worker=self.admin.worker).exists())
        self.assertEqual(TaskActivity.objects.get(task=unassigned, field="state_code").actor_id, self.admin.pk)

    def test_state_targets_keep_permissions_without_legacy_reason_responsible_or_dependency_rules(self):
        project = self.assigned_project(worker_state=True)
        task = self.task(project)
        Task.objects.filter(pk=task.pk).update(reason="Motivo histórico")
        original = self.state_values(task)
        for target in ("EN_CURSO", "BLOQUEADA", "COMPLETADA", "CANCELADA", "PENDIENTE"):
            count = TaskActivity.objects.filter(task=task, field="state_code").count()
            response = self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": target}, self.admin)
            self.assertEqual(response.status_code, 200, response.content)
            task.refresh_from_db()
            self.assertEqual(task.state_code, target)
            self.assertEqual(self.state_values(task), original)
            events = TaskActivity.objects.filter(task=task, field="state_code")
            self.assertEqual(events.count(), count + 1)
            self.assertEqual(events.latest("pk").actor_id, self.admin.pk)
        self.assertIsNone(task.responsible_id)
        self.assertFalse(ProjectMember.objects.filter(project=project, worker=self.admin.worker).exists())
        successor = self.task(project, responsible_id=self.worker.worker_id)
        TaskDependency.objects.create(project=project, predecessor=task, successor=successor)
        response = self.request("post", f"{project.pk}/tasks/{successor.pk}/state/", {"state_code": "EN_CURSO"}, self.worker)
        self.assertEqual(response.status_code, 200, response.content)
        successor.refresh_from_db()
        self.assertEqual(successor.state_code, "EN_CURSO")
        self.assertEqual(TaskActivity.objects.filter(task=successor, field="state_code").latest("pk").actor_id, self.worker.pk)
        self.assertTrue(TaskDependency.objects.filter(predecessor=task, successor=successor).exists())
        project.worker_state = False
        project.save(update_fields=["worker_state"])
        before = TaskActivity.objects.count()
        for user, target, expected in ((self.worker, "BLOQUEADA", 403), (self.outsider, "EN_CURSO", 404), (self.admin, "DESCONOCIDO", 400)):
            denied = self.request("post", f"{project.pk}/tasks/{successor.pk}/state/", {"state_code": target}, user)
            self.assertEqual(denied.status_code, expected, denied.content)
            successor.refresh_from_db()
            self.assertEqual(successor.state_code, "EN_CURSO")
            self.assertEqual(TaskActivity.objects.count(), before)
        allowed = self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "COMPLETADA"}, self.admin)
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def state_values(self, task):
        return Task.objects.filter(pk=task.pk).values(
            "project_id", "parent_id", "phase_id", "responsible_id", "name", "description",
            "priority", "start_date", "due_date", "estimated_minutes", "reason", "archived",
        ).get()

    def test_dependencies_allow_later_created_and_dated_predecessors_and_parent_tasks(self):
        project = self.project()
        first = self.task(project, due_date=str(self.today + timedelta(days=2)))
        later = self.task(project, due_date=str(self.today + timedelta(days=20)))
        self.task(project, parent_id=later.pk)
        response = self.request(
            "post", f"{project.pk}/tasks/{first.pk}/dependencies/", {"predecessor_id": later.pk}, self.admin
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(TaskDependency.objects.get(project=project).predecessor_id, later.pk)
        self.assertEqual(
            self.request(
                "delete", f"{project.pk}/tasks/{first.pk}/dependencies/", {"predecessor_id": later.pk}, self.admin
            ).status_code,
            204,
        )
        self.assertFalse(TaskDependency.objects.exists())

    def test_dependencies_reject_self_cross_project_subtasks_duplicate_and_cycles(self):
        project = self.project()
        first = self.task(project)
        second = self.task(project)
        third = self.task(project)
        child = self.task(project, parent_id=first.pk)
        foreign = self.task(self.project())
        for source, target, expected in (
            (first, first, 400),
            (foreign, first, 404),
            (child, first, 400),
            (first, child, 400),
        ):
            self.assertEqual(
                self.request(
                    "post", f"{project.pk}/tasks/{target.pk}/dependencies/", {"predecessor_id": source.pk}, self.admin
                ).status_code,
                expected,
            )
        for source, target in ((first, second), (second, third)):
            self.assertEqual(
                self.request(
                    "post", f"{project.pk}/tasks/{target.pk}/dependencies/", {"predecessor_id": source.pk}, self.admin
                ).status_code,
                201,
            )
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{first.pk}/dependencies/", {"predecessor_id": third.pk}, self.admin
            ).status_code,
            400,
        )
        self.assertEqual(
            self.request(
                "post", f"{project.pk}/tasks/{second.pk}/dependencies/", {"predecessor_id": first.pk}, self.admin
            ).status_code,
            400,
        )
        self.assertEqual(TaskDependency.objects.count(), 2)

    def test_workers_never_manage_dependencies_even_with_both_flags(self):
        project = self.assigned_project(worker_edit=True, worker_state=True)
        first = self.task(project, responsible_id=self.worker.worker_id)
        second = self.task(project, responsible_id=self.worker.worker_id)
        for method in ("post", "delete"):
            self.assertEqual(
                self.request(
                    method, f"{project.pk}/tasks/{second.pk}/dependencies/", {"predecessor_id": first.pk}, self.worker
                ).status_code,
                403,
            )

    def test_project_update_delete_and_task_delete_are_not_exposed(self):
        project = self.project()
        task = self.task(project)
        for method, path in (
            ("patch", f"{project.pk}/"),
            ("delete", f"{project.pk}/"),
            ("delete", f"{project.pk}/tasks/{task.pk}/"),
        ):
            self.assertEqual(self.request(method, path, {}, self.admin).status_code, 405)

    def test_cors_remains_explicit_and_allows_required_methods_only(self):
        from django.conf import settings

        response = self.client.options("/api/projects/", HTTP_ORIGIN=settings.FRONTEND_ORIGIN)
        self.assertIn("PATCH", response["Access-Control-Allow-Methods"])
        self.assertEqual(response["Access-Control-Allow-Origin"], settings.FRONTEND_ORIGIN)
        denied = self.client.options("/api/projects/", HTTP_ORIGIN="https://untrusted.invalid")
        self.assertNotIn("Access-Control-Allow-Origin", denied)

    def test_task_labels_fail_atomically_without_persisting_task(self):
        project = self.project(requirements=[{"name": "Base", "kind": "technical"}])
        requirement = ProjectRequirement.objects.get(project=project)
        response = self.request(
            "post",
            f"{project.pk}/tasks/",
            self.task_payload(requirement_ids=[requirement.pk], labels=[{"name": "base", "kind": "technical"}]),
            self.admin,
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(TaskLabel.objects.exists())

    def test_area_relationships_are_authoritative_over_legacy_primary_area(self):
        WorkerArea.objects.create(worker=self.worker.worker, area=self.other_area)
        project = self.project()
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.worker).status_code, 404)
        other = self.project(area_ids=[self.other_area.pk])
        self.assertEqual(self.request("get", f"{other.pk}/", user=self.worker).status_code, 200)
        candidates = self.request("get", f"workers/?area_ids={self.area.pk}", user=self.admin).json()
        self.assertNotIn(self.worker.worker_id, {item["id"] for item in candidates})

    def test_final_projects_reject_mutations_and_hide_management_actions(self):
        project = self.assigned_project(worker_edit=True, worker_state=True)
        task = self.task(project, responsible_id=self.worker.worker_id)
        project.state_code = "FINALIZADO"
        project.save()
        for user in (self.admin, self.worker):
            self.assertEqual(
                self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"name": "No"}, user).status_code, 400
            )
            response = self.request("get", f"{project.pk}/tasks/", user=user)
            self.assertFalse(response.json()[0]["permissions"]["edit"])
            self.assertFalse(response.json()[0]["permissions"]["state"])
            self.assertFalse(response.json()[0]["permissions"]["create_child"])

    def test_state_orm_is_atomic_preserves_attributes_and_authenticated_history(self):
        from apps.projects.services import audit, tasks

        project = self.assigned_project(worker_state=True)
        task = self.task(project, responsible_id=self.worker.worker_id, labels=[{"name": "QA", "kind": "technical"}])
        sibling = self.task(project)
        original = self.state_values(task)
        labels = list(TaskLabel.objects.filter(task=task).values("id", "name", "kind"))
        positions = dict(TaskOrder.objects.values_list("task_id", "position"))
        observed = []
        apply = tasks.apply_state_change

        def observed_apply(*args, **kwargs):
            context = audit.operation.get()
            observed.append((connection.in_atomic_block, context[0].pk, context[1]))
            return apply(*args, **kwargs)

        with patch.object(tasks, "apply_state_change", side_effect=observed_apply):
            result = tasks.change_task_state(self.worker, project.pk, task.pk, {"state_code": "EN_CURSO", "reason": ""})
        self.assertEqual(result.state_code, "EN_CURSO")
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][0])
        self.assertEqual(observed[0][1], self.worker.pk)
        event = TaskActivity.objects.get(task=task, field="state_code")
        self.assertEqual(event.actor_id, self.worker.pk)
        self.assertEqual(event.actor_name, f"{self.worker.worker.first_names} {self.worker.worker.last_names}")
        self.assertEqual(event.operation_id, observed[0][2])
        self.assertEqual(event.old_value["code"], "PENDIENTE")
        self.assertEqual(event.new_value["code"], "EN_CURSO")
        self.assertEqual(self.state_values(task), original)
        self.assertEqual(list(TaskLabel.objects.filter(task=task).values("id", "name", "kind")), labels)
        self.assertEqual(dict(TaskOrder.objects.values_list("task_id", "position")), positions)
        sibling.refresh_from_db()
        self.assertEqual(sibling.state_code, "PENDIENTE")
        self.assertIsNone(audit.operation.get())

    def test_state_history_failure_rolls_back_task_progress_timestamp_and_audit_context(self):
        from apps.projects.services import audit, tasks

        project = self.assigned_project(worker_state=True)
        task = self.task(project, responsible_id=self.worker.worker_id)
        original = Task.objects.filter(pk=task.pk).values().get()
        positions = dict(TaskOrder.objects.values_list("task_id", "position"))
        events = list(TaskActivity.objects.values())
        record = tasks.record_state_change
        inserted = []

        def fail_after_history(*args):
            record(*args)
            inserted.append(TaskActivity.objects.filter(task=task, field="state_code").count())
            raise RuntimeError("Fallo QA posterior al historial")

        with patch.object(tasks, "record_state_change", side_effect=fail_after_history):
            with self.assertRaisesRegex(RuntimeError, "Fallo QA"):
                tasks.change_task_state(self.worker, project.pk, task.pk, {"state_code": "COMPLETADA", "reason": ""})
        self.assertEqual(inserted, [1])
        self.assertEqual(Task.objects.filter(pk=task.pk).values().get(), original)
        self.assertEqual(list(TaskActivity.objects.values()), events)
        self.assertEqual(dict(TaskOrder.objects.values_list("task_id", "position")), positions)
        self.assertIsNone(audit.operation.get())


class ProjectPolicyTests(unittest.TestCase):
    def test_project_end_date_remains_nullable_in_model_migration_state_and_sql(self):
        self.assertTrue(Project._meta.get_field("end_date").null)
        field = MigrationLoader(None).project_state().apps.get_model("proyectos", "Project")._meta.get_field("end_date")
        self.assertTrue(field.null)
        policy = import_module("apps.projects.migrations.0003_project_sql_policy")
        structure = policy.source("sp_validar_estructura_actividad")
        self.assertIn("p.fecha_fin IS NULL OR p_fin<=p.fecha_fin", structure)
        self.assertIn("p_inicio>=f.fecha_inicio AND p_fin<=f.fecha_fin", structure)
        self.assertIn("p_fin<=fecha_fin", structure)

    def test_creation_permission_migration_preserves_old_flags_and_has_guarded_reverse(self):
        migration = import_module("apps.projects.migrations.0005_project_worker_create")
        self.assertIn("NOT NULL DEFAULT FALSE", migration.ADD_PERMISSION_SQL)
        self.assertNotIn("trabajador_edita_tareas", migration.ADD_PERMISSION_SQL)
        self.assertNotIn("trabajador_cambia_estado", migration.ADD_PERMISSION_SQL)
        field = (
            MigrationLoader(None)
            .project_state()
            .apps.get_model("proyectos", "Project")
            ._meta.get_field("worker_create")
        )
        self.assertEqual(field.column, "trabajador_crea_tareas")
        self.assertFalse(field.default)
        editor = unittest.mock.MagicMock()
        editor.connection.vendor = "mysql"
        cursor = editor.connection.cursor.return_value.__enter__.return_value
        migration.install(None, editor)
        cursor.execute.assert_called_once_with(migration.ADD_PERMISSION_SQL)
        cursor.reset_mock()
        cursor.fetchone.return_value = (1,)
        with self.assertRaises(RuntimeError):
            migration.restore(None, editor)
        self.assertEqual(cursor.execute.call_count, 1)
        cursor.reset_mock()
        cursor.fetchone.return_value = (0,)
        migration.restore(None, editor)
        self.assertEqual(cursor.execute.call_args_list[-1].args, (migration.REMOVE_PERMISSION_SQL,))

    def test_cycle_detection_does_not_depend_on_order(self):
        self.assertTrue(dependency_would_cycle([(3, 2), (2, 1)], 1, 3))
        self.assertFalse(dependency_would_cycle([(3, 2)], 1, 3))

    def test_sql_policy_preserves_deliverable_and_hierarchy_checks(self):
        policy = import_module("apps.projects.migrations.0003_project_sql_policy")
        state = policy.source("sp_cambiar_estado_actividad")
        self.assertIn("v_estado_entregable", state)
        self.assertIn("Hay subtareas incompatibles", state)
        self.assertIn("trabajador_cambia_estado=1", state)
        self.assertIn("NOT(v_responsable<=>v_persona)", state)
        self.assertIn("proyecto_area", state)
        dependencies = policy.source("sp_agregar_dependencia")
        self.assertNotIn("s.fecha_inicio<p.fecha_fin", dependencies)
        self.assertIn("padre_id IS NOT NULL", dependencies)
        self.assertIn("v_ciclo>0", dependencies)
        self.assertIn("rol_codigo='ADMINISTRADOR'", dependencies)

    def test_schema_reverse_refuses_data_loss(self):
        schema = import_module("apps.projects.migrations.0002_project_schema")
        with patch("django.db.connection.cursor") as cursor_factory:
            cursor = cursor_factory.return_value
            cursor.fetchone.return_value = (1,)
            with self.assertRaises(RuntimeError):
                schema.ensure_unused(cursor)

    def test_audit_identity_is_authenticated_and_cleared_after_failure(self):
        from apps.projects.services.audit import audit_actor

        with patch("apps.projects.services.audit.connection") as database:
            database.vendor = "mysql"
            with patch("apps.projects.services.audit.connection.cursor") as cursor_factory:
                actor = unittest.mock.Mock(pk=41, global_role="ADMINISTRADOR")
                with self.assertRaises(ValueError):
                    with audit_actor(actor):
                        raise ValueError("synthetic failure")
                cursor = cursor_factory.return_value.__enter__.return_value
                statement, values = cursor.execute.call_args_list[0].args
                self.assertEqual(statement, "SET @gm_usuario_id = %s, @gm_operacion_id = %s")
                self.assertEqual(values[0], 41)
                self.assertEqual(len(values[1]), 36)
                self.assertEqual(cursor.execute.call_args_list[-1].args, ("SET @gm_usuario_id = NULL, @gm_operacion_id = NULL, @gm_estados_anteriores = NULL",))
