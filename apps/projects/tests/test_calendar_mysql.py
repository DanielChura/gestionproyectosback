from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
import json
import os
from pathlib import Path
from threading import Event
import time
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.utils import timezone

from apps.authentication.tokens import issue_access
from apps.projects.models import Task, TaskActivity, TaskCalendarIntent
from apps.projects.services import calendar
from . import test_calendar as cases
from . import test_state_policy_integration_mysql as fixtures


migration = import_module("apps.projects.migrations.0017_task_calendar_guard")


class CalendarMySQLTests(cases.CalendarContractTests):
    legacy = False

    @classmethod
    def setUpClass(cls):
        fixtures.StatePolicyIntegrationMySQLTests.setUpClass.__func__(cls)
        fixture = cls()
        fixtures.StatePolicyIntegrationMySQLTests.setUp(fixture)
        project, task = fixture.seed()
        project.start_date = fixture.today - timedelta(days=10)
        project.save(update_fields=["start_date"])
        from apps.projects.services.audit import audit_actor
        with audit_actor(fixture.admin):
            Task.objects.filter(pk=task.pk).update(start_date=fixture.today - timedelta(days=2))
        with connection.cursor() as cursor:
            cursor.execute("SELECT id,fecha_inicio,fecha_fin FROM actividad ORDER BY id")
            old = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) FROM actividad_historial")
            count = cursor.fetchone()[0]
            cursor.execute("ALTER TABLE actividad DROP COLUMN calendario_revision")
        state = MigrationLoader(None).project_state([("proyectos", "0016_task_state_policy_consistency")])
        with patch.dict(os.environ, {"TASK_CALENDAR_MIGRATION_MAINTENANCE": "1"}):
            cls.migration_state = MigrationExecutor(connection).apply_migration(state, migration.Migration("0017_task_calendar_guard", "proyectos"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT id,fecha_inicio,fecha_fin FROM actividad ORDER BY id")
            if cursor.fetchall() != old:
                raise RuntimeError("Upgrade changed historical dates")
            cursor.execute("SELECT COUNT(*) FROM actividad WHERE calendario_revision<>1")
            if cursor.fetchone()[0]:
                raise RuntimeError("Upgrade did not initialize revisions")
            cursor.execute("SELECT COUNT(*) FROM actividad_historial")
            if cursor.fetchone()[0] != count:
                raise RuntimeError("Upgrade fabricated history")
        cls.upgrade_verified = True
        fixtures.StatePolicyIntegrationMySQLTests.tearDown(fixture)
        cls.evidence({"scenario": "upgrade_0016_to_0017", "historical_dates_preserved": True, "initial_revision": "1", "history_unchanged": True})

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER gm_bu_actividad_calendario")
        with connection.schema_editor() as editor:
            editor.delete_model(TaskCalendarIntent)
        fixtures.StatePolicyIntegrationMySQLTests.tearDownClass.__func__(cls)

    setUp = fixtures.StatePolicyIntegrationMySQLTests.setUp

    def tearDown(self):
        TaskCalendarIntent.objects.all().delete()
        fixtures.StatePolicyIntegrationMySQLTests.tearDown(self)

    @classmethod
    def evidence(cls, value):
        target = os.environ.get("GM_CALENDAR_EVIDENCE")
        if target:
            with Path(target).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    def race(self, task, payloads, actors=None):
        held, attempted, release = Event(), Event(), Event()
        identities, timeline = {}, []
        actors = actors or [self.admin, self.admin]
        tokens = [issue_access(user, timezone.now() + timedelta(minutes=5)) for user in actors]

        def run(index):
            db = connections["default"]
            db.close()
            db.ensure_connection()
            with db.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                identities[index] = cursor.fetchone()[0]
            intercepted = False

            def synchronize(execute, sql, params, many, context):
                nonlocal intercepted
                normalized = sql.replace(chr(96), "").upper()
                target = not intercepted and "FOR UPDATE" in normalized and "FROM PROYECTO " in normalized
                if target:
                    intercepted = True
                    timeline.append({"request": index, "attempt": time.monotonic()})
                    if index == 1:
                        attempted.set()
                result = execute(sql, params, many, context)
                if target and index == 0:
                    held.set()
                    if not release.wait(15):
                        raise RuntimeError("First calendar request was not released")
                return result

            try:
                with db.execute_wrapper(synchronize):
                    result = Client().patch(f"/api/projects/{task.project_id}/tasks/{task.pk}/", data=json.dumps(payloads[index]),
                                            content_type="application/json", HTTP_AUTHORIZATION="Bearer " + tokens[index])
                with db.cursor() as cursor:
                    cursor.execute("SELECT @gm_usuario_id,@gm_operacion_id")
                    self.assertEqual(cursor.fetchone(), (None, None))
                timeline.append({"request": index, "response": time.monotonic(), "status": result.status_code})
                return result
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, 0)
            try:
                self.assertTrue(held.wait(10), "First request did not acquire project lock")
                second = pool.submit(run, 1)
                self.assertTrue(attempted.wait(10), "Second request did not attempt project lock")
                waiting = False
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM performance_schema.data_lock_waits w JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID WHERE r.PROCESSLIST_ID=%s AND b.PROCESSLIST_ID=%s", [identities[1], identities[0]])
                        waiting = cursor.fetchone() is not None
                    if waiting:
                        break
                    time.sleep(0.02)
                self.assertTrue(waiting, "No actual overlapping lock wait observed")
            finally:
                release.set()
            responses = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(len(set(identities.values())), 2)
        self.evidence({"scenario": self._testMethodName, "connections": identities, "lock_wait_observed": waiting,
                       "timeline": timeline, "statuses": [r.status_code for r in responses]})
        return responses


    def test_concurrent_same_revision_has_one_winner(self):
        project, task = self.seed()
        a = self.payload(task, start_date=str(self.today + timedelta(days=1)))
        b = self.payload(task, start_date=str(self.today + timedelta(days=2)))
        count = TaskActivity.objects.count()
        responses = self.race(task, [a, b])
        self.assertEqual([r.status_code for r in responses], [200, 409])
        self.assertEqual(responses[1].json()["code"], "calendar_revision_conflict")
        self.assertEqual(TaskCalendarIntent.objects.count(), 1)
        self.assertEqual(TaskActivity.objects.count(), count + 1)

    def test_concurrent_duplicate_intention_is_one_write_and_two_acknowledgements(self):
        project, task = self.seed()
        data = self.payload(task, start_date=str(self.today + timedelta(days=1)), due_date=None)
        count = TaskActivity.objects.count()
        replies = self.race(task, [data, data])
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertFalse(replies[0].json()["calendar_receipt"]["replayed"])
        self.assertTrue(replies[1].json()["calendar_receipt"]["replayed"])
        self.assertEqual(TaskActivity.objects.count(), count + 2)
        self.assertEqual(TaskCalendarIntent.objects.count(), 1)
        item = TaskCalendarIntent.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskCalendarIntent.objects.create(actor=item.actor, task=task, operation=item.operation, key=item.key,
                                             payload_hash=item.payload_hash, expected_revision=1, expires_at=item.expires_at)
        self.assertEqual(self.snapshot(task)["calendar_revision"], "2")

    def test_concurrent_reused_payload_is_distinguishable(self):
        project, task = self.seed()
        data = self.payload(task, due_date=None)
        responses = self.race(task, [data, {**data, "due_date": str(self.today + timedelta(days=11))}])
        self.assertEqual([r.status_code for r in responses], [200, 409])
        self.assertEqual(responses[1].json()["code"], "calendar_intent_reused")
        self.assertEqual(TaskCalendarIntent.objects.count(), 1)

    def test_concurrent_actor_isolation_does_not_replay_another_users_success(self):
        project, task = self.seed()
        data = self.payload(task, due_date=None)
        responses = self.race(task, [data, data], [self.admin, self.worker])
        self.assertEqual([r.status_code for r in responses], [200, 409])
        self.assertEqual(responses[1].json()["code"], "calendar_revision_conflict")
        self.assertEqual(TaskCalendarIntent.objects.get().actor_id, self.admin.pk)

    def test_concurrent_rollback_releases_key_without_false_success(self):
        project, task = self.seed()
        data = self.payload(task, due_date=None)
        original = calendar.finish_calendar_edit
        failed = Event()
        count = TaskActivity.objects.count()

        def fail_once(current, intent):
            if not failed.is_set():
                failed.set()
                raise IntegrityError("Synthetic calendar rollback after history")
            return original(current, intent)

        with patch.object(calendar, "finish_calendar_edit", side_effect=fail_once):
            responses = self.race(task, [data, data])
        self.assertEqual([r.status_code for r in responses], [409, 200])
        self.assertFalse(responses[1].json()["calendar_receipt"]["replayed"])
        self.assertEqual(TaskCalendarIntent.objects.count(), 1)
        self.assertEqual(TaskActivity.objects.count(), count + 1)

    def test_external_date_update_advances_revision_and_nondate_update_cannot_reset_it(self):
        from apps.projects.services.audit import audit_actor
        project, task = self.seed()
        payload = self.payload(task, due_date=None)
        with audit_actor(self.admin):
            Task.objects.filter(pk=task.pk).update(start_date=self.today + timedelta(days=1))
        task.refresh_from_db()
        self.assertEqual(task.calendar_revision, 2)
        Task.objects.filter(pk=task.pk).update(calendar_revision=1, priority=4)
        task.refresh_from_db()
        self.assertEqual(task.calendar_revision, 2)
        self.assertEqual(self.edit(task, payload).json()["code"], "calendar_revision_conflict")

    def test_phase_validation_and_history_are_not_bypassed(self):
        project, task = self.seed()
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO fase(id,proyecto_id,archivada,fecha_inicio,fecha_fin) VALUES(1,%s,0,%s,%s)",
                           [project.pk, self.today, self.today + timedelta(days=12)])
            cursor.execute("UPDATE actividad SET fase_id=1 WHERE id=%s", [task.pk])
        count = TaskActivity.objects.count()
        invalid = self.edit(task, self.payload(task, due_date=str(self.today + timedelta(days=13))))
        self.assertEqual(invalid.status_code, 409)
        self.assertFalse(TaskCalendarIntent.objects.exists())
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(self.snapshot(task)["calendar_revision"], "1")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE actividad SET fase_id=NULL WHERE id=%s", [task.pk])
            cursor.execute("DELETE FROM fase WHERE id=1")

    def test_upgrade_preflight_rejects_existing_schema_without_touching_data(self):
        self.assertTrue(self.upgrade_verified)
        project, task = self.seed()
        snapshot = self.snapshot(task)
        with patch.dict(os.environ, {"TASK_CALENDAR_MIGRATION_MAINTENANCE": "1"}), self.assertRaises(RuntimeError):
            migration.preflight(connection)
        self.assertEqual(self.snapshot(task), snapshot)

    def test_migration_rollback_guard_and_ddl_failure_restore_previous_baseline(self):
        project, task = self.seed()
        with connection.cursor() as cursor:
            cursor.execute("SELECT id,fecha_inicio,fecha_fin FROM actividad ORDER BY id")
            dates = cursor.fetchall()
        with patch.dict(os.environ, {"TASK_CALENDAR_MIGRATION_MAINTENANCE": "1"}), connection.schema_editor() as editor:
            migration.uninstall(self.migration_state.apps, editor)
            from django.db import DatabaseError
            try:
                with patch.object(migration, "trigger_sql", return_value="INVALID SYNTHETIC SQL"), self.assertRaises(DatabaseError):
                    migration.install(self.migration_state.apps, editor)
                self.assertNotIn("actividad_calendario_intento", connection.introspection.table_names())
                with connection.cursor() as cursor:
                    self.assertNotIn("calendario_revision", {c.name for c in connection.introspection.get_table_description(cursor, "actividad")})
            finally:
                migration.install(self.migration_state.apps, editor)
        with connection.cursor() as cursor:
            cursor.execute("SELECT id,fecha_inicio,fecha_fin FROM actividad ORDER BY id")
            self.assertEqual(cursor.fetchall(), dates)
        self.assertEqual(self.edit(task, self.payload(task, due_date=None)).status_code, 200)
        with patch.dict(os.environ, {"TASK_CALENDAR_MIGRATION_MAINTENANCE": "1"}), connection.schema_editor() as editor, self.assertRaises(RuntimeError):
            migration.uninstall(self.migration_state.apps, editor)

