from datetime import timedelta
from importlib import import_module
import json
import os
from pathlib import Path
import re

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from apps.authentication.models import MigrationBackup
from apps.projects.models import Task, TaskActivity
from apps.projects.services.audit import audit_actor
from . import test_calendar_mysql as calendar_cases
from . import test_state_policy_compatibility as compatibility
from . import test_state_policy_integration_mysql as state_cases


policy = import_module("apps.projects.migrations.0016_task_state_policy_consistency")


def prepare_captured_definitions(cls):
    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Captured SQL is allowed only on the isolated test database")
    path = os.environ.get("GM_STATE_POLICY_SOURCE_BACKUP")
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        definitions = {name: raw[name] for name in policy.NAMES}
    else:
        definitions = {
            policy.direct.NAME: compatibility.ansi_definition(policy.baseline.source(policy.direct.NAME), policy.direct.NAME),
            policy.direct.MODE: compatibility.ansi_definition(policy.direct.source(), policy.direct.MODE),
        }
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@SESSION.sql_mode")
        cls.pre_capture_sql_mode = cursor.fetchone()[0]
        cursor.execute("SET SESSION sql_mode=%s", [definitions[policy.direct.NAME]["sql_mode"]])
        for name, definition in definitions.items():
            policy.reviewed(definition, name)
            sql = re.sub(r"\ACREATE\s+DEFINER\s*=\s*.+?\s+PROCEDURE", "CREATE PROCEDURE",
                         definition["Create Procedure"], count=1, flags=re.IGNORECASE)
            policy.helpers.recreate(cursor, name, sql, definition)
        cls.captured_definitions = {
            name: policy.helpers.read_definition(cursor, "PROCEDURE", name) for name in policy.NAMES
        }


class CapturedStatePolicyMySQLTests(state_cases.StatePolicyIntegrationMySQLTests):
    prepare_policy_definitions = classmethod(prepare_captured_definitions)

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION sql_mode=%s", [cls.pre_capture_sql_mode])

    def test_backup_has_exact_pre_install_definitions_and_metadata(self):
        saved = MigrationBackup.objects.get(key=policy.KEY).payload["procedures"]
        self.assertEqual(saved, self.captured_definitions)
        for name, definition in self.definitions().items():
            self.assertEqual(definition["sql_mode"], saved[name]["sql_mode"])
            self.assertEqual(definition["character_set_client"], saved[name]["character_set_client"])
            self.assertEqual(definition["collation_connection"], saved[name]["collation_connection"])
            old_header = policy.definition_header(saved[name], name)
            new_header = policy.definition_header(definition, name)
            self.assertEqual((old_header["user"], old_header["host"]), (new_header["user"], new_header["host"]))

    def test_recovery_from_captured_pair_preserves_existing_dates_states_and_history(self):
        project = self.assigned_project()
        project.start_date = self.today - timedelta(days=10)
        project.save(update_fields=["start_date"])
        parent = self.task(project)
        child = self.task(project, parent_id=parent.pk)
        with audit_actor(self.admin):
            Task.objects.filter(pk=parent.pk).update(start_date=self.today - timedelta(days=3), reason="Histórico QA")
            Task.objects.filter(pk=child.pk).update(start_date=self.today - timedelta(days=2), due_date=None)
        before = list(Task.objects.order_by("pk").values())
        history = list(TaskActivity.objects.order_by("pk").values())
        with connection.schema_editor() as editor:
            policy.restore(state_cases.apps, editor)
            self.assertEqual(self.definitions(), self.captured_definitions)
            policy.install(state_cases.apps, editor)
        self.assertEqual(list(Task.objects.order_by("pk").values()), before)
        self.assertEqual(list(TaskActivity.objects.order_by("pk").values()), history)

    def test_reinstallation_keeps_original_backup_and_record(self):
        before = MigrationBackup.objects.get(key=policy.KEY).payload
        recorded = list(MigrationRecorder(connection).migration_qs.filter(
            app="proyectos", name="0016_task_state_policy_consistency",
        ).values_list("id", "applied"))
        with connection.schema_editor() as editor:
            policy.install(state_cases.apps, editor)
        self.assertEqual(MigrationBackup.objects.get(key=policy.KEY).payload, before)
        self.assertEqual(list(MigrationRecorder(connection).migration_qs.filter(
            app="proyectos", name="0016_task_state_policy_consistency",
        ).values_list("id", "applied")), recorded)


class CapturedCalendarMySQLTests(calendar_cases.CalendarMySQLTests):
    prepare_policy_definitions = classmethod(prepare_captured_definitions)

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION sql_mode=%s", [cls.pre_capture_sql_mode])

    def test_calendar_upgraded_after_captured_policy_and_kept_backup(self):
        self.assertTrue(self.upgrade_verified)
        applied = MigrationRecorder(connection).applied_migrations()
        for name in ("0015_task_state_without_assignee", "0016_task_state_policy_consistency", "0017_task_calendar_guard"):
            self.assertIn(("proyectos", name), applied)
        self.assertEqual(
            MigrationBackup.objects.get(key=policy.KEY).payload["procedures"],
            self.captured_definitions,
        )
