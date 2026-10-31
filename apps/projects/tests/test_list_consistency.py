import unittest
from unittest.mock import patch

from apps.projects.models import ProjectMember, Task, TaskActivity, TaskOrder
from . import test_project_integrations as fixtures


class ListConsistencyTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectIntegrationTests.tearDownClass.__func__)
    setUp = fixtures.ProjectIntegrationTests.setUp
    tearDown = fixtures.ProjectIntegrationTests.tearDown
    account = fixtures.ProjectIntegrationTests.account
    request = fixtures.ProjectIntegrationTests.request
    project_payload = fixtures.ProjectIntegrationTests.project_payload
    project = fixtures.ProjectIntegrationTests.project
    assigned_project = fixtures.ProjectIntegrationTests.assigned_project
    task_payload = fixtures.ProjectIntegrationTests.task_payload
    task = fixtures.ProjectIntegrationTests.task
    snapshot = fixtures.ProjectIntegrationTests.snapshot
    move = fixtures.ProjectIntegrationTests.move

    def manual(self, project, task, state, user=None):
        return self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": state}, user or self.admin)

    def test_parent_and_child_roundtrips_share_identity_metadata_and_audit(self):
        project = self.assigned_project()
        parent = self.task(project)
        child = self.task(project, parent_id=parent.pk)
        for task in (parent, child):
            Task.objects.filter(pk=task.pk).update(reason="Motivo histórico conservado")
            original = self.snapshot(project, task)
            for operation in (self.move, self.manual, self.move, self.manual):
                for state in ("EN_CURSO", "PENDIENTE"):
                    previous = TaskActivity.objects.filter(task=task, field="state_code").count()
                    response = operation(project, task, state)
                    self.assertEqual(response.status_code, 200, response.content)
                    saved = self.snapshot(project, task)
                    task.refresh_from_db()
                    self.assertEqual(saved["state_code"], task.state_code)
                    self.assertEqual(task.reason, "Motivo histórico conservado")
                    expected = next(item for item in self.request("get", f"{project.pk}/task-states/", user=self.admin).json()["states"] if item["code"] == state)
                    for field in ("name", "category", "color"):
                        self.assertEqual(saved[f"state_{field}"], expected[field])
                    for field in ("name", "description", "parent_id", "responsible_id", "assignees", "start_date", "due_date", "labels", "estimated_minutes", "priority"):
                        self.assertEqual(saved[field], original[field], field)
                    self.assertEqual(TaskActivity.objects.filter(task=task, field="state_code").count(), previous + 1)
                    self.assertIn("EN_CURSO" if state == "PENDIENTE" else "PENDIENTE", [item["target"] for item in saved["transitions"]])
                    if task.pk == parent.pk:
                        child.refresh_from_db()
                        self.assertEqual(child.state_code, "PENDIENTE")

    def test_manual_keeps_position_and_noop_has_no_state_event(self):
        project = self.assigned_project()
        task = self.task(project)
        position = TaskOrder.objects.get(task=task).position
        self.assertEqual(self.manual(project, task, "EN_CURSO").status_code, 200)
        count = TaskActivity.objects.count()
        self.assertEqual(self.manual(project, task, "EN_CURSO").status_code, 200)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(TaskOrder.objects.get(task=task).position, position)

    def test_three_levels_keep_independent_states_parents_and_sibling_order_after_roundtrips(self):
        project = self.assigned_project()
        parent = self.task(project)
        child = self.task(project, parent_id=parent.pk)
        sibling = self.task(project, parent_id=parent.pk)
        grandchild = self.task(project, parent_id=child.pk)
        self.assertEqual(self.manual(project, child, "EN_CURSO").status_code, 200)
        self.assertEqual(self.manual(project, grandchild, "BLOQUEADA").status_code, 200)
        order = list(TaskOrder.objects.filter(task__parent=parent).order_by("position").values_list("task_id", flat=True))
        for operation in (self.manual, self.move, self.manual, self.move):
            for state in ("EN_REVISION", "PENDIENTE"):
                self.assertEqual(operation(project, parent, state).status_code, 200)
                child.refresh_from_db()
                grandchild.refresh_from_db()
                self.assertEqual((child.state_code, child.parent_id), ("EN_CURSO", parent.pk))
                self.assertEqual((grandchild.state_code, grandchild.parent_id), ("BLOQUEADA", child.pk))
        for state in ("PENDIENTE", "EN_CURSO"):
            self.assertEqual(self.move(project, child, state).status_code, 200)
            parent.refresh_from_db()
            grandchild.refresh_from_db()
            sibling.refresh_from_db()
            self.assertEqual(parent.state_code, "PENDIENTE")
            self.assertEqual(grandchild.state_code, "BLOQUEADA")
            self.assertEqual(sibling.state_code, "PENDIENTE")
        self.assertEqual(list(TaskOrder.objects.filter(task__parent=parent).order_by("position").values_list("task_id", flat=True)), order)

    def test_conflict_foreign_parent_and_failure_do_not_leave_state_order_or_history(self):
        project = self.assigned_project()
        task, other = self.task(project), self.task(project)
        child = self.task(project, parent_id=task.pk)
        revision = self.snapshot(project, task)["move_revision"]
        self.assertEqual(self.move(project, other, "EN_CURSO").status_code, 200)
        count = TaskActivity.objects.count()
        self.assertEqual(self.move(project, task, "EN_CURSO", revision=revision).status_code, 409)
        self.assertEqual(self.move(project, child, "EN_CURSO", before=other.pk).status_code, 400)
        before = dict(TaskOrder.objects.values_list("task_id", "position"))
        with patch("apps.projects.services.ordering.record_activity", side_effect=RuntimeError("Synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.move(project, task, "EN_CURSO", before=other.pk)
        task.refresh_from_db()
        self.assertEqual(task.state_code, "PENDIENTE")
        self.assertEqual(dict(TaskOrder.objects.values_list("task_id", "position")), before)
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_readonly_revoked_and_foreign_state_are_rejected_for_both_operations(self):
        project = self.assigned_project()
        task = self.task(project, responsible_id=self.worker.worker_id)
        count = TaskActivity.objects.count()
        for operation in (self.move, self.manual):
            self.assertEqual(operation(project, task, "EN_CURSO", user=self.worker).status_code, 403)
            self.assertEqual(operation(project, task, "NOT_A_STATE").status_code, 400)
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=False)
        for operation in (self.move, self.manual):
            self.assertEqual(operation(project, task, "EN_CURSO", user=self.worker).status_code, 404)
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_sibling_order_before_between_after_and_empty_state(self):
        project = self.assigned_project()
        parent = self.task(project)
        one, two, three = [self.task(project, parent_id=parent.pk) for _ in range(3)]
        for task, before in ((three, one.pk), (three, two.pk), (one, None)):
            self.assertEqual(self.move(project, task, "PENDIENTE", before).status_code, 200)
        self.assertEqual(self.move(project, three, "BLOQUEADA").status_code, 200)
        self.assertEqual(self.move(project, three, "PENDIENTE", two.pk).status_code, 200)
        listing = self.request("get", f"{project.pk}/tasks/", user=self.admin).json()
        self.assertEqual([item["id"] for item in listing if item["parent_id"] == parent.pk], [three.pk, two.pk, one.pk])
