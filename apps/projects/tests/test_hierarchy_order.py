import unittest
from unittest.mock import patch

from apps.projects.models import Task, TaskOrder, TaskActivity
from apps.projects.services.ordering import move_task
from . import test_projects as fixtures


class HierarchyOrderTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectFlowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectFlowTests.tearDownClass.__func__)
    setUp = fixtures.ProjectFlowTests.setUp
    tearDown = fixtures.ProjectFlowTests.tearDown
    account = fixtures.ProjectFlowTests.account
    request = fixtures.ProjectFlowTests.request
    project_payload = fixtures.ProjectFlowTests.project_payload
    project = fixtures.ProjectFlowTests.project
    assigned_project = fixtures.ProjectFlowTests.assigned_project
    task_payload = fixtures.ProjectFlowTests.task_payload
    task = fixtures.ProjectFlowTests.task

    def move(self, project, task, parent, before=None, **extra):
        current = self.request("get", f"{project.pk}/tasks/{task.pk}/", user=self.admin).json()["task"]
        data = dict(mode="hierarchy", state_code=task.state_code, parent_id=parent, before_id=before, expected_revision=current["move_revision"])
        data.update(extra)
        return self.request("post", f"{project.pk}/tasks/{task.pk}/move/", data, self.admin)

    def test_move_branch_preserves_descendants_calendar_and_state(self):
        project = self.assigned_project()
        first, second = self.task(project), self.task(project)
        child = self.task(project, parent_id=first.pk, state_code="EN_CURSO")
        leaf = self.task(project, parent_id=child.pk)
        before = list(Task.objects.values("id", "state_code", "start_date", "due_date", "calendar_revision"))
        response = self.move(project, child, second.pk)
        self.assertEqual(response.status_code, 200, response.content)
        child.refresh_from_db()
        self.assertEqual(child.parent_id, second.pk)
        self.assertEqual(Task.objects.get(pk=leaf.pk).parent_id, child.pk)
        self.assertEqual(before, list(Task.objects.values("id", "state_code", "start_date", "due_date", "calendar_revision")))
        self.assertTrue(TaskActivity.objects.filter(task=child, field="parent_id").exists())

    def test_state_contract_and_hierarchy_reject_incompatible_intents(self):
        project = self.assigned_project()
        first, second = self.task(project), self.task(project)
        child = self.task(project, parent_id=first.pk)
        for source, parent in ((first, second.pk), (child, None), (child, child.pk)):
            self.assertEqual(self.move(project, source, parent).status_code, 400)
        self.assertEqual(self.move(project, child, first.pk, state_code="EN_CURSO").status_code, 400)
        self.assertEqual(self.move(project, child, first.pk, mode="state").status_code, 400)
        self.assertEqual(self.move(project, first, None, before=child.pk).status_code, 400)

    def test_parent_order_and_audit_rollback_together(self):
        project = self.assigned_project()
        first, second = self.task(project), self.task(project)
        child = self.task(project, parent_id=first.pk)
        current = self.request("get", f"{project.pk}/tasks/{child.pk}/", user=self.admin).json()["task"]
        order = list(TaskOrder.objects.values())
        history = TaskActivity.objects.count()
        with patch("apps.projects.services.ordering.record_activity", side_effect=RuntimeError("QA rollback")):
            with self.assertRaises(RuntimeError):
                move_task(self.admin, project.pk, child.pk, dict(mode="hierarchy", parent_id=second.pk, before_id=None, state_code=child.state_code, expected_revision=current["move_revision"]))
        self.assertEqual(Task.objects.get(pk=child.pk).parent_id, first.pk)
        self.assertEqual(list(TaskOrder.objects.values()), order)
        self.assertEqual(TaskActivity.objects.count(), history)
