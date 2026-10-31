import os
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

from apps.projects.models import ProjectMember, Task, TaskAssignee, TaskDependency, TaskStateDefinition, TaskStatusTemplate, TaskActivity, TaskRelation, ProjectTeam, TaskOrder
from apps.projects.services.audit import audit_actor
from . import test_state_templates as template_tests
from . import test_projects as fixtures


migration = import_module("apps.projects.migrations.0010_task_status_templates")
SQL_ROOT = Path(__file__).resolve().parents[2]


class StateTemplatesMySQLTests(unittest.TestCase):
    account = fixtures.ProjectFlowTests.account
    request = fixtures.ProjectFlowTests.request
    project_payload = fixtures.ProjectFlowTests.project_payload
    project = fixtures.ProjectFlowTests.project
    assigned_project = fixtures.ProjectFlowTests.assigned_project
    task_payload = fixtures.ProjectFlowTests.task_payload
    task = fixtures.ProjectFlowTests.task
    configuration = template_tests.StateTemplateTests.configuration
    templates = template_tests.StateTemplateTests.templates
    preset = template_tests.StateTemplateTests.preset
    apply = template_tests.StateTemplateTests.apply

    @classmethod
    def setUpClass(cls):
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_templates":
            raise unittest.SkipTest("Requires dedicated isolated MySQL template settings")
        if connection.vendor != "mysql" or connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
            raise RuntimeError("Requires the authorized synthetic loopback database")
        if connection.introspection.table_names():
            raise RuntimeError("The dedicated database must be empty; existing tables will not be removed")
        previous = MigrationLoader(None).project_state([("proyectos", "0009_task_assignees")])
        labels = (
            "organization.Area", "organization.Specialty", "workers.Worker", "workers.WorkerArea", "teams.Team", "teams.TeamMember", "autenticacion.User",
            "autenticacion.Role", "autenticacion.UserRole", "autenticacion.AuthToken", "proyectos.ProjectType",
            "proyectos.ProjectState", "proyectos.TaskState", "proyectos.TaskTransition", "proyectos.Project",
            "proyectos.ProjectFavorite", "proyectos.ProjectMember", "proyectos.ProjectRequirement", "proyectos.ProjectTaskStatus",
            "proyectos.Task", "proyectos.TaskAssignee", "proyectos.TaskDependency", "proyectos.TaskLabel", "autenticacion.MigrationBackup",
        )
        cls.labels = labels
        with connection.schema_editor() as editor:
            for label in labels:
                if label.startswith("teams."):
                    editor.create_model(apps.get_model(label))
                    continue
                historical = previous.apps.get_model(label)
                editor.create_model(historical if historical._meta.managed else apps.get_model(label))
        MigrationRecorder(connection).ensure_schema()
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE actividad ADD CONSTRAINT test_actividad_estado_fk FOREIGN KEY(estado_codigo) REFERENCES estado_actividad(codigo)")
            cursor.execute("CREATE TABLE test_entregable (actividad_id BIGINT NOT NULL, obligatorio BOOLEAN NOT NULL, estado_revision VARCHAR(30) NOT NULL)")
            cursor.execute("CREATE SQL SECURITY INVOKER VIEW v_estado_entregable AS SELECT * FROM test_entregable")
            cursor.execute("CREATE TABLE registro_tiempo (actividad_id BIGINT NOT NULL, anulado BOOLEAN NOT NULL)")
            cursor.execute((SQL_ROOT / "authentication/migrations/sql/v_trabajador_area_autorizada.sql").read_text(encoding="utf-8"))
            cursor.execute(migration.baseline.source())
            cursor.execute(migration.structure.source("sp_validar_estructura_actividad"))
            cursor.execute(migration.structure.source("bu_actividad_estructura"))
        with patch.dict(os.environ, {"TASK_STATUS_MIGRATION_MAINTENANCE": "1"}):
            instance = migration.Migration("0010_task_status_templates", "proyectos")
            MigrationExecutor(connection).apply_migration(previous, instance)
        with connection.schema_editor() as editor:
            editor.create_model(TaskRelation)
            editor.create_model(TaskActivity)
        cls.tables = [apps.get_model(label) for label in labels] + [TaskStateDefinition, TaskStatusTemplate, TaskRelation, TaskActivity]
        integration = import_module("apps.projects.migrations.0013_project_teams_specialties_order")
        state = MigrationLoader(None).project_state([("proyectos", "0012_task_detail"), ("teams", "0001_initial")])
        with patch.dict(os.environ, {"PROJECT_INTEGRATION_MAINTENANCE": "1"}):
            MigrationExecutor(connection).apply_migration(state, integration.Migration("0013_project_teams_specialties_order", "proyectos"))
        cls.tables += [ProjectTeam, TaskOrder]
        direct = import_module("apps.projects.migrations.0014_task_direct_move")
        state = MigrationLoader(None).project_state([("proyectos", "0013_project_teams_specialties_order")])
        with patch.dict(os.environ, {"TASK_MOVE_MIGRATION_MAINTENANCE": "1"}):
            MigrationExecutor(connection).apply_migration(state, direct.Migration("0014_task_direct_move", "proyectos"))

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER bu_actividad_estructura")
            cursor.execute("DROP PROCEDURE sp_aplicar_estado_actividad_modo")
            for name in migration.ROUTINES + ("sp_aplicar_estado_actividad",):
                cursor.execute(f"DROP PROCEDURE {name}")
            cursor.execute("DROP FUNCTION fn_regla_estado_actividad")
            cursor.execute("DROP VIEW v_estado_entregable")
            cursor.execute("DROP VIEW v_trabajador_area_autorizada")
            cursor.execute("DROP TABLE test_entregable")
            cursor.execute("DROP TABLE registro_tiempo")
            cursor.execute("DROP TABLE django_migrations")
        with connection.schema_editor() as editor:
            for model in reversed(cls.tables):
                editor.delete_model(model)

    def setUp(self):
        fixtures.ProjectFlowTests.setUp(self)

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM test_entregable")
            for model in reversed(self.tables):
                if model._meta.label == "autenticacion.MigrationBackup":
                    continue
                if model is Task:
                    while Task.objects.exists():
                        parents = list(Task.objects.exclude(parent_id=None).values_list("parent_id", flat=True))
                        Task.objects.exclude(pk__in=parents).delete()
                    continue
                cursor.execute(f"DELETE FROM `{model._meta.db_table}`")

    test_presets = template_tests.StateTemplateTests.test_exact_presets_empty_done_and_scrum_active_completed
    test_template_persistence = template_tests.StateTemplateTests.test_template_save_update_copy_persistence_without_applying
    test_template_scope = template_tests.StateTemplateTests.test_template_validation_authentication_scope_and_preset_immutability
    test_automatic_mapping = template_tests.StateTemplateTests.test_used_state_maps_automatically_and_keeps_subtasks_assignments
    test_identity = template_tests.StateTemplateTests.test_identity_category_color_and_empty_states_survive_edits
    test_scrum = template_tests.StateTemplateTests.test_scrum_completed_is_active_and_closure_uses_current_permissions
    test_foreign_state = template_tests.StateTemplateTests.test_foreign_custom_codes_cannot_be_applied_or_created
    test_inherited_rules = template_tests.StateTemplateTests.test_custom_transitions_ignore_discarded_reason_and_supervision_rules

    def call(self, user, project, task, target, reason=""):
        with audit_actor(user), connection.cursor() as cursor:
            cursor.callproc("sp_cambiar_estado_actividad", [project.pk, task.pk, target, reason])
            while cursor.nextset():
                pass
        task.refresh_from_db()

    def test_sql_rejects_unapproved_deliverables_without_partial_configuration(self):
        project = self.assigned_project()
        states = self.apply(project, self.preset(project)).json()["states"]
        task = self.task(project, state_code=states[1]["code"], responsible_id=self.worker.worker_id)
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO test_entregable VALUES(%s,1,'PENDIENTE')", [task.pk])
        before = self.configuration(project)
        replacement = self.preset(project)
        result = self.apply(project, replacement, [{"source": states[1]["code"], "target": "draft_2", "reason": ""}])
        self.assertEqual(result.status_code, 409, result.content)
        self.assertEqual(self.configuration(project), before)
        task.refresh_from_db()
        self.assertEqual(task.state_code, states[1]["code"])
        with connection.cursor() as cursor:
            cursor.execute("UPDATE test_entregable SET estado_revision='APROBADO'")
        self.call(self.admin, project, task, states[2]["code"])
        self.assertEqual(task.progress, 100)
        self.assertFalse(task.archived)

    def test_inner_sql_does_not_commit_outer_transaction(self):
        project = self.assigned_project()
        states = self.apply(project, self.preset(project)).json()["states"]
        task = self.task(project, state_code=states[0]["code"], responsible_id=self.worker.worker_id)
        with self.assertRaises(RuntimeError):
            with audit_actor(self.admin), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.callproc("sp_aplicar_estado_actividad", [project.pk, task.pk, states[1]["code"], ""])
                    while cursor.nextset():
                        pass
                raise RuntimeError("Synthetic rollback")
        task.refresh_from_db()
        self.assertEqual(task.state_code, states[0]["code"])

    def test_sql_permissions_and_cross_project_destination(self):
        project = self.assigned_project(worker_state=True)
        states = self.apply(project, self.preset(project)).json()["states"]
        other = self.project()
        foreign = self.apply(other, self.preset(other)).json()["states"]
        task = self.task(project, state_code=states[0]["code"], responsible_id=self.worker.worker_id)
        for user, target in ((self.outsider, states[1]["code"]), (self.worker, states[2]["code"]), (self.admin, foreign[1]["code"])):
            with self.assertRaises(DatabaseError):
                self.call(user, project, task, target)
        task.refresh_from_db()
        self.assertEqual(task.state_code, states[0]["code"])

    def test_sql_custom_hierarchy_dependencies_and_secondary_assignees(self):
        project = self.assigned_project(worker_state=True)
        states = self.apply(project, self.preset(project)).json()["states"]
        pending, active, closed = [item["code"] for item in states]
        parent = self.task(project, state_code=active, responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk, state_code=pending, responsible_id=self.worker.worker_id)
        with self.assertRaises(DatabaseError):
            self.call(self.admin, project, parent, closed)
        self.call(self.admin, project, child, closed)
        self.call(self.admin, project, parent, closed)
        with self.assertRaises(DatabaseError):
            self.call(self.admin, project, child, active, "Reabrir")
        successor = self.task(project, state_code=pending, responsible_id=self.worker.worker_id)
        TaskDependency.objects.create(project=project, predecessor=parent, successor=successor)
        self.call(self.worker, project, successor, active)
        with self.assertRaises(DatabaseError):
            self.call(self.admin, project, parent, active, "Reabrir")
        colleague = self.account("secondary", "TRABAJADOR", self.area)
        ProjectMember.objects.create(project=project, worker=colleague.worker)
        extra = self.task(project, state_code=pending, responsible_id=self.worker.worker_id)
        TaskAssignee.objects.create(task=extra, worker=colleague.worker)
        response = self.request("post", f"{project.pk}/tasks/{extra.pk}/state/", {"state_code": active}, colleague)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["assignees"]), 2)

    def test_migration_rollback_guard_and_routine_restoration(self):
        TaskStatusTemplate.objects.create(project=self.project(), name="Preservar", states=[])
        with connection.schema_editor() as editor:
            with self.assertRaises(RuntimeError):
                migration.restore(apps, editor)
        TaskStatusTemplate.objects.all().delete()
        direct = import_module("apps.projects.migrations.0014_task_direct_move")
        with patch.dict(os.environ, {"TASK_STATUS_MIGRATION_MAINTENANCE": "1", "TASK_MOVE_MIGRATION_MAINTENANCE": "1"}), connection.schema_editor() as editor:
            direct.restore(apps, editor)
            migration.restore(apps, editor)
            with connection.cursor() as cursor:
                migration.review(cursor)
            migration.install(apps, editor)
            with connection.cursor() as cursor:
                migration.review(cursor, installed=True)
            direct.install(apps, editor)
