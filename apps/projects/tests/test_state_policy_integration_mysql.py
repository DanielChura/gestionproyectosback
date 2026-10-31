from importlib import import_module
import os
import unittest
from unittest.mock import patch

from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

from apps.projects.models import ProjectMember, ProjectState, Task, TaskActivity, TaskOrder
from apps.projects.services.audit import audit_actor
from . import test_list_consistency as cases
from . import test_task_detail_mysql as fixtures
from . import test_state_templates as template_cases


policy = import_module("apps.projects.migrations.0016_task_state_policy_consistency")


class StatePolicyIntegrationMySQLTests(unittest.TestCase):
    legacy = False
    account = cases.ListConsistencyTests.account
    request = cases.ListConsistencyTests.request
    project_payload = cases.ListConsistencyTests.project_payload
    project = cases.ListConsistencyTests.project
    assigned_project = cases.ListConsistencyTests.assigned_project
    task_payload = cases.ListConsistencyTests.task_payload
    task = cases.ListConsistencyTests.task
    snapshot = cases.ListConsistencyTests.snapshot
    manual = cases.ListConsistencyTests.manual
    configuration = template_cases.StateTemplateTests.configuration
    templates = template_cases.StateTemplateTests.templates
    preset = template_cases.StateTemplateTests.preset
    apply = template_cases.StateTemplateTests.apply

    @classmethod
    def setUpClass(cls):
        fixtures.TaskDetailMySQLTests.setUpClass.__func__(cls)
        state = MigrationLoader(None).project_state([("proyectos", "0014_task_direct_move")])
        with patch.dict(os.environ, {"TASK_MOVE_MIGRATION_MAINTENANCE": "1"}):
            if cls.legacy:
                old = {name: policy.historical(name, True, True) for name in policy.NAMES}
                without_assignee = policy.historical(policy.direct.MODE, True)
                with connection.cursor() as cursor:
                    for name, sql in old.items():
                        original = policy.helpers.read_definition(cursor, "PROCEDURE", name)
                        policy.helpers.recreate(cursor, name, policy.direct.with_definer(sql, original["Create Procedure"]), original)
                with patch.object(policy.direct, "source", return_value=old[policy.direct.MODE]), patch.object(policy.previous, "source", return_value=without_assignee):
                    MigrationExecutor(connection).apply_migration(state, policy.previous.Migration("0015_task_state_without_assignee", "proyectos"))
            else:
                MigrationExecutor(connection).apply_migration(state, policy.previous.Migration("0015_task_state_without_assignee", "proyectos"))
        prepare = getattr(cls, "prepare_policy_definitions", None)
        if prepare is not None:
            prepare()
        state = MigrationLoader(None).project_state([("proyectos", "0015_task_state_without_assignee")])
        with patch.dict(os.environ, {"TASK_STATE_POLICY_MAINTENANCE": "1"}):
            MigrationExecutor(connection).apply_migration(state, policy.Migration("0016_task_state_policy_consistency", "proyectos"))

    tearDownClass = classmethod(fixtures.TaskDetailMySQLTests.tearDownClass.__func__)
    setUp = fixtures.TaskDetailMySQLTests.setUp

    tearDown = fixtures.TaskDetailMySQLTests.tearDown

    def move(self, project, task, target, before=None, revision=None, user=None):
        return self.request("post", f"{project.pk}/tasks/{task.pk}/move/", {
            "state_code": target, "before_id": before,
            "expected_revision": revision or self.snapshot(project, task)["move_revision"],
        }, user or self.admin)

    def sql(self, project, task, target, user=None, mode=False):
        with audit_actor(user or self.admin), transaction.atomic(), connection.cursor() as cursor:
            cursor.callproc(policy.direct.MODE if mode else policy.direct.NAME,
                            [project.pk, task.pk, target, ""] + ([True] if mode else []))
            while cursor.nextset():
                pass

    def definitions(self):
        with connection.cursor() as cursor:
            return {name: policy.helpers.read_definition(cursor, "PROCEDURE", name) for name in policy.NAMES}

    def values(self, task):
        task.refresh_from_db()
        return {name: getattr(task, name) for name in (
            "parent_id", "responsible_id", "name", "description", "priority", "start_date", "due_date", "estimated_minutes", "reason", "archived",
        )}

    def test_manual_move_and_nested_sql_share_targets_and_preserve_attributes(self):
        project = self.assigned_project()
        parent = self.task(project)
        child = self.task(project, parent_id=parent.pk)
        for task in (parent, child):
            Task.objects.filter(pk=task.pk).update(reason="Motivo histórico conservado", estimated_minutes=95)
            original = self.values(task)
            for operation in (self.manual, self.move, self.sql, lambda *args: self.sql(*args, mode=True)):
                for target in ("EN_CURSO", "BLOQUEADA", "COMPLETADA", "CANCELADA", "PENDIENTE"):
                    snapshot = self.snapshot(project, task)
                    self.assertEqual(set(snapshot["move_targets"]), {item["target"] for item in snapshot["transitions"]})
                    count = TaskActivity.objects.filter(task=task, field="state_code").count()
                    response = operation(project, task, target)
                    if response is not None:
                        self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(self.values(task), original)
                    self.assertEqual(task.state_code, target)
                    events = TaskActivity.objects.filter(task=task, field="state_code")
                    self.assertEqual(events.count(), count + 1)
                    self.assertEqual(events.latest("pk").actor_id, self.admin.pk)

    def test_worker_readonly_and_enabled_targets_match_all_paths(self):
        project = self.assigned_project()
        task = self.task(project, responsible_id=self.worker.worker_id)
        count = TaskActivity.objects.count()
        for operation in (self.manual, self.move):
            self.assertEqual(operation(project, task, "COMPLETADA", user=self.worker).status_code, 403)
        with self.assertRaises(DatabaseError):
            self.sql(project, task, "COMPLETADA", self.worker)
        self.assertEqual(TaskActivity.objects.count(), count)
        project.worker_state = True
        project.save(update_fields=["worker_state"])
        for operation in (self.manual, self.move, self.sql):
            for target in ("COMPLETADA", "PENDIENTE"):
                result = operation(project, task, target, user=self.worker)
                if result is not None:
                    self.assertEqual(result.status_code, 200, result.content)
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=False)
        for operation in (self.manual, self.move):
            self.assertEqual(operation(project, task, "EN_CURSO", user=self.worker).status_code, 404)
        with self.assertRaises(DatabaseError):
            self.sql(project, task, "EN_CURSO", self.worker)

    def test_configured_and_foreign_destinations_remain_isolated(self):
        project, other = self.assigned_project(), self.project()
        result = self.apply(project, self.preset(project)).json()
        foreign = self.apply(other, self.preset(other)).json()["states"][1]["code"]
        task = self.task(project, state_code=result["states"][0]["code"])
        count = TaskActivity.objects.count()
        for target in ("EN_CURSO", "NOT_A_STATE", foreign):
            for operation in (self.manual, self.move):
                self.assertEqual(operation(project, task, target).status_code, 400)
            with self.assertRaises(DatabaseError):
                self.sql(project, task, target)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(task.state_code, self.snapshot(project, task)["state_code"])

    def test_outer_rollback_conflict_and_noop_never_commit_partial_activity(self):
        project = self.assigned_project()
        task, sibling = self.task(project), self.task(project)
        revision = self.snapshot(project, task)["move_revision"]
        positions = dict(TaskOrder.objects.values_list("task_id", "position"))
        count = TaskActivity.objects.count()
        for mode in (False, True):
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.sql(project, task, "EN_CURSO", mode=mode)
                raise RuntimeError("Synthetic outer rollback")
            task.refresh_from_db()
            self.assertEqual(task.state_code, "PENDIENTE")
            self.assertEqual(TaskActivity.objects.count(), count)
        self.sql(project, task, "PENDIENTE")
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(self.move(project, sibling, "EN_CURSO").status_code, 200)
        count = TaskActivity.objects.count()
        self.assertEqual(self.move(project, task, "EN_CURSO", revision=revision).status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.state_code, "PENDIENTE")
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(TaskOrder.objects.get(task=task).position, positions[task.pk])

    def test_migration_guards_failure_and_restore_preserve_reviewed_definitions(self):
        with patch.dict(os.environ, {"TASK_STATE_POLICY_MAINTENANCE": "0"}), connection.schema_editor() as editor:
            with self.assertRaises(RuntimeError):
                policy.install(apps, editor)
        with patch.dict(os.environ, {"TASK_STATE_POLICY_MAINTENANCE": "1"}), connection.schema_editor() as editor:
            policy.restore(apps, editor)
            before = self.definitions()
            recreate = policy.helpers.recreate
            failed = False

            def fail_second(cursor, name, definition, metadata=None):
                nonlocal failed
                if name == policy.direct.MODE and not failed:
                    failed = True
                    raise DatabaseError("Synthetic DDL failure")
                return recreate(cursor, name, definition, metadata)

            with patch.object(policy.helpers, "recreate", side_effect=fail_second), self.assertRaises(DatabaseError):
                policy.install(apps, editor)
            self.assertEqual(self.definitions(), before)
            policy.install(apps, editor)
            with connection.cursor() as cursor:
                current = policy.helpers.read_definition(cursor, "PROCEDURE", policy.direct.MODE)
                drift = current["Create Procedure"].replace("END", "SELECT 1; END")
                recreate(cursor, policy.direct.MODE, drift, current)
            drifted = self.definitions()
            with self.assertRaises(RuntimeError):
                policy.install(apps, editor)
            self.assertEqual(self.definitions(), drifted)
            with self.assertRaises(RuntimeError):
                policy.restore(apps, editor)
            with connection.cursor() as cursor:
                recreate(cursor, policy.direct.MODE, current["Create Procedure"], current)
            for name, definition in self.definitions().items():
                policy.reviewed(definition, name, installed=True)

    def test_closed_archived_and_unauthenticated_sql_are_rejected(self):
        project = self.assigned_project()
        task = self.task(project)
        with transaction.atomic(), connection.cursor() as cursor, self.assertRaises(DatabaseError):
            cursor.callproc(policy.direct.NAME, [project.pk, task.pk, "EN_CURSO", ""])
        ProjectState.objects.create(code="FINALIZADO", name="Finalizado")
        project.state_code = "FINALIZADO"
        project.save(update_fields=["state_code"])
        count = TaskActivity.objects.count()
        for operation in (self.manual, self.move):
            self.assertEqual(operation(project, task, "EN_CURSO").status_code, 400)
        with self.assertRaises(DatabaseError):
            self.sql(project, task, "EN_CURSO")
        project.state_code = "PLANIFICADO"
        project.save(update_fields=["state_code"])
        revision = self.snapshot(project, task)["move_revision"]
        Task.objects.filter(pk=task.pk).update(archived=True)
        self.assertEqual(self.manual(project, task, "EN_CURSO").status_code, 404)
        self.assertEqual(self.move(project, task, "EN_CURSO", revision=revision).status_code, 404)
        with self.assertRaises(DatabaseError):
            self.sql(project, task, "EN_CURSO")
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_recorded_migrations_and_sql_have_no_internal_commit_or_reason_requirement(self):
        applied = MigrationRecorder(connection).applied_migrations()
        for name in ("0015_task_state_without_assignee", "0016_task_state_policy_consistency"):
            self.assertIn(("proyectos", name), applied)
        for name, definition in self.definitions().items():
            policy.reviewed(definition, name, installed=True)
            self.assertNotIn("COMMIT", policy.source(name))
            self.assertNotIn("START TRANSACTION", policy.source(name))
            self.assertNotIn("Debe indicar el motivo", policy.source(name))


class StatePolicyUpgradeMySQLTests(StatePolicyIntegrationMySQLTests):
    legacy = True
