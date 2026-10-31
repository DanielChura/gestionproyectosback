from datetime import timedelta
from io import StringIO
import unittest
from uuid import uuid4
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.projects.models import Task, TaskActivity, TaskCalendarIntent, TaskDependency
from . import test_projects as fixtures


class CalendarContractTests(unittest.TestCase):
    account = fixtures.ProjectFlowTests.account
    request = fixtures.ProjectFlowTests.request
    project_payload = fixtures.ProjectFlowTests.project_payload
    project = fixtures.ProjectFlowTests.project
    assigned_project = fixtures.ProjectFlowTests.assigned_project
    task_payload = fixtures.ProjectFlowTests.task_payload
    task = fixtures.ProjectFlowTests.task

    setUpClass = classmethod(fixtures.ProjectFlowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectFlowTests.tearDownClass.__func__)
    setUp = fixtures.ProjectFlowTests.setUp
    tearDown = fixtures.ProjectFlowTests.tearDown

    def seed(self):
        project = self.assigned_project(worker_edit=True)
        task = self.task(project, responsible_id=self.worker.worker_id)
        return project, task

    def payload(self, task, key=None, **changes):
        task.refresh_from_db()
        return {"expected_calendar_revision": str(task.calendar_revision), "calendar_intent_id": str(key or uuid4()), **changes}

    def edit(self, task, data, user=None):
        return self.request("patch", f"{task.project_id}/tasks/{task.pk}/", data, user or self.admin)

    def snapshot(self, task, user=None):
        return self.request("get", f"{task.project_id}/tasks/{task.pk}/", user=user or self.admin).json()["task"]

    def test_preconditions_required_and_metadata_only_rejected(self):
        project, task = self.seed()
        original = self.snapshot(task)
        for data, field in (({"due_date": None}, "expected_calendar_revision"),
                            ({"due_date": None, "expected_calendar_revision": "1"}, "calendar_intent_id"),
                            ({"due_date": None, "expected_calendar_revision": 0, "calendar_intent_id": str(uuid4())}, "expected_calendar_revision"),
                            ({"due_date": None, "expected_calendar_revision": 1, "calendar_intent_id": "invalid"}, "calendar_intent_id"),
                            ({"calendar_intent_id": str(uuid4())}, "calendar_intent_id")):
            response = self.edit(task, data)
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn(field, response.json())
        self.assertEqual(self.snapshot(task), original)
        self.assertFalse(TaskCalendarIntent.objects.exists())
        listing = self.request("get", f"{project.pk}/tasks/", user=self.admin).json()
        self.assertEqual(listing[0]["calendar_revision"], "1")

    def test_noncalendar_changes_do_not_conflict_and_omitted_extremes_come_from_locked_row(self):
        project, task = self.seed()
        payload = self.payload(task, start_date=str(self.today + timedelta(days=1)))
        self.assertEqual(self.edit(task, {"name": "Nombre vigente"}).status_code, 200)
        changed = self.edit(task, payload)
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(changed.json()["calendar_revision"], "2")
        self.assertEqual(changed.json()["name"], "Nombre vigente")
        self.assertEqual(changed.json()["due_date"], str(self.today + timedelta(days=10)))
        removed = self.edit(task, self.payload(task, due_date=None))
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["start_date"], payload["start_date"])
        self.assertIsNone(removed.json()["due_date"])

    def test_aba_invalidates_old_revision_without_history_or_receipt(self):
        project, task = self.seed()
        original = self.payload(task, start_date=str(self.today))
        self.assertEqual(self.edit(task, self.payload(task, start_date=str(self.today + timedelta(days=1)))).status_code, 200)
        self.assertEqual(self.edit(task, self.payload(task, start_date=str(self.today))).status_code, 200)
        count = TaskActivity.objects.count()
        stale = self.edit(task, original)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "calendar_revision_conflict")
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(TaskCalendarIntent.objects.count(), 2)
        self.assertEqual(self.snapshot(task)["calendar_revision"], "3")

    def test_retry_success_after_later_edit_returns_current_calendar_without_rewriting(self):
        project, task = self.seed()
        first = self.payload(task, start_date=str(self.today + timedelta(days=1)))
        self.assertEqual(self.edit(task, first).status_code, 200)
        self.assertEqual(self.edit(task, self.payload(task, start_date=str(self.today + timedelta(days=2)))).status_code, 200)
        before = self.snapshot(task)
        count = TaskActivity.objects.count()
        retry = self.edit(task, first)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()["calendar_receipt"], {"intent_id": first["calendar_intent_id"], "applied_revision": "2", "replayed": True})
        self.assertEqual(retry.json()["calendar_revision"], "3")
        self.assertEqual(retry.json()["start_date"], before["start_date"])
        self.assertEqual(self.snapshot(task), before)
        self.assertEqual(TaskActivity.objects.count(), count)
        conflict = self.edit(task, {**first, "due_date": None})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "calendar_intent_reused")

    def test_actor_and_resource_scope_and_replay_rechecks_permissions(self):
        project, task = self.seed()
        key = uuid4()
        payload = self.payload(task, key, due_date=None)
        self.assertEqual(self.edit(task, payload).status_code, 200)
        conflict = self.edit(task, payload, self.worker)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "calendar_revision_conflict")
        worker_payload = self.payload(task, key, start_date=str(self.today + timedelta(days=1)))
        self.assertEqual(self.edit(task, worker_payload, self.worker).status_code, 200)
        project.worker_edit = False
        project.save(update_fields=["worker_edit"])
        self.assertEqual(self.edit(task, worker_payload, self.worker).status_code, 403)
        other = self.task(project, name="Otro recurso")
        self.assertEqual(self.edit(other, self.payload(other, key, due_date=None)).status_code, 200)
        self.assertEqual(TaskCalendarIntent.objects.count(), 3)
        self.assertEqual(self.edit(task, payload, self.outsider).status_code, 404)
        self.assertEqual(self.edit(task, payload, user=None).status_code, 200)
        anonymous = self.request("patch", f"{project.pk}/tasks/{task.pk}/", payload)
        self.assertEqual(anonymous.status_code, 401)

    def test_rollback_after_history_does_not_record_success_and_same_key_can_retry(self):
        project, task = self.seed()
        previous = self.snapshot(task)
        payload = self.payload(task, name="No conservar", start_date=str(self.today + timedelta(days=1)))
        count = TaskActivity.objects.count()
        with patch("apps.projects.services.calendar.finish_calendar_edit", side_effect=IntegrityError("Synthetic after history")):
            self.assertEqual(self.edit(task, payload).status_code, 409)
        self.assertEqual(self.snapshot(task), previous)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertFalse(TaskCalendarIntent.objects.exists())
        self.assertEqual(self.edit(task, payload).status_code, 200)

    def test_noop_keeps_timestamp_revision_and_history_but_confirms_intention(self):
        project, task = self.seed()
        before = self.snapshot(task)
        count = TaskActivity.objects.count()
        payload = self.payload(task, start_date=before["start_date"], due_date=before["due_date"])
        response = self.edit(task, payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["calendar_revision"], "1")
        self.assertEqual(self.snapshot(task), before)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(self.edit(task, payload).json()["calendar_receipt"]["replayed"], True)
        self.assertEqual(TaskCalendarIntent.objects.count(), 1)

    def test_family_and_project_validation_preserve_everything(self):
        project, parent = self.seed()
        child = self.task(project, parent_id=parent.pk, start_date=str(self.today + timedelta(days=2)), due_date=str(self.today + timedelta(days=8)))
        grandchild = self.task(project, parent_id=child.pk, start_date=str(self.today + timedelta(days=3)), due_date=str(self.today + timedelta(days=7)))
        before = {t.pk: self.snapshot(t) for t in (parent, child, grandchild)}
        count = TaskActivity.objects.count()
        for task, fields in ((parent, {"due_date": str(self.today + timedelta(days=6))}),
                             (parent, {"start_date": str(self.today + timedelta(days=3))}),
                             (child, {"start_date": str(self.today + timedelta(days=4))}),
                             (child, {"due_date": str(self.today + timedelta(days=11))}),
                             (child, {"due_date": str(self.today - timedelta(days=1))}),
                             (parent, {"due_date": str(self.today + timedelta(days=40))})):
            response = self.edit(task, self.payload(task, **fields))
            self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual({t.pk: self.snapshot(t) for t in (parent, child, grandchild)}, before)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertFalse(TaskCalendarIntent.objects.exists())

    def test_historical_dates_and_one_day_null_windows_do_not_propagate(self):
        project, parent = self.seed()
        project.start_date = self.today - timedelta(days=40)
        project.save(update_fields=["start_date"])
        Task.objects.filter(pk=parent.pk).update(start_date=self.today - timedelta(days=20))
        historic = self.today - timedelta(days=10)
        result = self.edit(parent, self.payload(parent, start_date=str(historic), due_date=str(historic)))
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["start_date"], result.json()["due_date"])
        dependent = self.task(project, name="Dependiente intacta")
        TaskDependency.objects.create(project=project, predecessor=parent, successor=dependent)
        before = self.snapshot(dependent)
        self.assertEqual(self.edit(parent, self.payload(parent, due_date=None)).status_code, 200)
        after = self.snapshot(dependent)
        self.assertEqual({k: v for k, v in after.items() if k != "move_revision"}, {k: v for k, v in before.items() if k != "move_revision"})
        self.assertIsNone(self.snapshot(parent)["due_date"])

    def test_expired_and_pending_keys_never_become_silent_new_intentions(self):
        project, task = self.seed()
        payload = self.payload(task, due_date=None)
        self.assertEqual(self.edit(task, payload).status_code, 200)
        TaskCalendarIntent.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.edit(task, payload).status_code, 410)
        call_command("expire_calendar_intents", stdout=StringIO(), batch_size=1)
        self.assertEqual(TaskCalendarIntent.objects.get().status, "expired")
        self.assertEqual(self.edit(task, payload).json()["code"], "calendar_intent_expired")
        TaskCalendarIntent.objects.update(status="pending", expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.edit(task, payload).json()["code"], "calendar_intent_pending")
        self.assertEqual(TaskCalendarIntent.objects.count(), 1)

    def test_pair_is_atomic_and_audited_once_per_changed_field(self):
        project, task = self.seed()
        count = TaskActivity.objects.count()
        response = self.edit(task, self.payload(task, start_date=str(self.today + timedelta(days=1)), due_date=str(self.today + timedelta(days=11))))
        self.assertEqual(response.status_code, 200, response.content)
        events = list(TaskActivity.objects.order_by("id")[count:])
        self.assertEqual([event.field for event in events], ["start_date", "due_date"])
        self.assertEqual(len({event.operation_id for event in events}), 1)
        self.assertEqual({event.actor_id for event in events}, {self.admin.pk})
        self.assertEqual(response.json()["calendar_revision"], "2")

