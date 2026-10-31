from importlib import import_module
import os
from unittest.mock import patch

from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

from apps.projects.models import TaskActivity, TaskRelation
from apps.projects.services.audit import audit_actor
from . import test_task_detail as cases
from . import test_quick_tasks_mysql as fixtures
from . import test_state_templates as template_cases
from . import test_state_templates_mysql as mysql_cases


migration = import_module("apps.projects.migrations.0012_task_detail")


class TaskDetailMySQLTests(cases.TaskDetailTests):
    configuration = template_cases.StateTemplateTests.configuration
    templates = template_cases.StateTemplateTests.templates
    preset = template_cases.StateTemplateTests.preset
    apply = template_cases.StateTemplateTests.apply
    call = mysql_cases.StateTemplatesMySQLTests.call
    test_existing_sql_deliverables = mysql_cases.StateTemplatesMySQLTests.test_sql_rejects_unapproved_deliverables_without_partial_configuration
    test_existing_sql_permissions = mysql_cases.StateTemplatesMySQLTests.test_sql_permissions_and_cross_project_destination
    test_existing_sql_hierarchy_dependencies = mysql_cases.StateTemplatesMySQLTests.test_sql_custom_hierarchy_dependencies_and_secondary_assignees
    test_existing_scrum_supervision = template_cases.StateTemplateTests.test_scrum_completed_is_active_and_closure_uses_current_permissions
    test_existing_reason_supervision = template_cases.StateTemplateTests.test_custom_transitions_ignore_discarded_reason_and_supervision_rules

    @classmethod
    def setUpClass(cls):
        fixtures.QuickTasksMySQLTests.setUpClass.__func__(cls)
        with connection.schema_editor() as editor:
            editor.delete_model(TaskActivity)
            editor.delete_model(TaskRelation)
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE actividad DROP COLUMN duracion_estimada_minutos")
        state = MigrationLoader(None).project_state([("proyectos", "0011_task_optional_properties")])
        with patch.dict(os.environ, {"TASK_DETAIL_MIGRATION_MAINTENANCE": "1"}):
            MigrationExecutor(connection).apply_migration(state, migration.Migration("0012_task_detail", "proyectos"))

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER gm_au_actividad_historial")
        fixtures.QuickTasksMySQLTests.tearDownClass.__func__(cls)

    setUp = fixtures.QuickTasksMySQLTests.setUp
    tearDown = fixtures.QuickTasksMySQLTests.tearDown

    def sql_state(self, project, task, code, nested=False):
        with connection.cursor() as cursor:
            cursor.callproc("sp_aplicar_estado_actividad" if nested else "sp_cambiar_estado_actividad", [project.pk, task.pk, code, ""])
            while cursor.nextset():
                pass

    def test_actual_sql_procedure_audits_once_and_rolls_back_with_outer_transaction(self):
        project = self.assigned_project()
        task = self.task(project, responsible_id=self.worker.worker_id)
        count = TaskActivity.objects.count()
        with audit_actor(self.admin):
            self.sql_state(project, task, "EN_CURSO")
        events = TaskActivity.objects.filter(task=task, field="state_code")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().old_value["code"], "PENDIENTE")
        self.assertEqual(events.get().new_value["code"], "EN_CURSO")
        with self.assertRaises(RuntimeError), audit_actor(self.admin), transaction.atomic():
            self.sql_state(project, task, "EN_REVISION", nested=True)
            raise RuntimeError("Synthetic rollback")
        task.refresh_from_db()
        self.assertEqual(task.state_code, "EN_CURSO")
        self.assertEqual(TaskActivity.objects.count(), count + 1)
        with connection.cursor() as cursor:
            cursor.execute("SELECT @gm_usuario_id, @gm_operacion_id, @gm_estados_anteriores")
            self.assertEqual(cursor.fetchone(), (None, None, None))

    def test_flow_reassignment_keeps_previous_name_and_operation_identity(self):
        project = self.assigned_project()
        states = self.apply(project, self.preset(project, "kanban")).json()["states"]
        task = self.task(project, state_code=states[1]["code"], responsible_id=self.worker.worker_id)
        result = self.apply(project, self.preset(project, "normal"), [{"source": states[1]["code"], "target": "draft_1", "reason": ""}])
        self.assertEqual(result.status_code, 200, result.content)
        event = TaskActivity.objects.get(task=task, field="state_code")
        self.assertEqual(event.old_value["name"], states[1]["name"])
        self.assertNotEqual(event.old_value["code"], event.new_value["code"])
        self.assertEqual(event.actor_id, self.admin.pk)

    def test_migration_guards_and_database_duration_constraint(self):
        task = self.task(self.project())
        with connection.schema_editor() as editor:
            with patch.dict(os.environ, {"TASK_DETAIL_MIGRATION_MAINTENANCE": "0"}), self.assertRaises(RuntimeError):
                migration.uninstall(apps, editor)
            with patch.dict(os.environ, {"TASK_DETAIL_MIGRATION_MAINTENANCE": "1"}), self.assertRaises(RuntimeError):
                migration.uninstall(apps, editor)
        with connection.cursor() as cursor:
            for value in (0, -1):
                with self.assertRaises(DatabaseError):
                    cursor.execute("UPDATE actividad SET duracion_estimada_minutos=%s WHERE id=%s", [value, task.pk])
            cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s", [migration.TRIGGER])
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute("SELECT COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='actividad' AND COLUMN_NAME='duracion_estimada_minutos'")
            self.assertEqual(cursor.fetchone(), ("int unsigned", "YES"))

    def test_empty_migration_reversal_and_forward_restore_real_schema(self):
        state = MigrationLoader(None).project_state([("proyectos", "0011_task_optional_properties")])
        instance = migration.Migration("0012_task_detail", "proyectos")
        executor = MigrationExecutor(connection)
        with patch.dict(os.environ, {"TASK_DETAIL_MIGRATION_MAINTENANCE": "1"}):
            executor.unapply_migration(state, instance)
            self.assertNotIn("actividad_historial", connection.introspection.table_names())
            self.assertNotIn("actividad_relacion", connection.introspection.table_names())
            with connection.cursor() as cursor:
                self.assertNotIn("duracion_estimada_minutos", {column.name for column in connection.introspection.get_table_description(cursor, "actividad")})
            executor.apply_migration(state, instance)
        self.assertIn("actividad_historial", connection.introspection.table_names())
        self.assertIn("actividad_relacion", connection.introspection.table_names())
