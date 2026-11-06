import unittest
from unittest.mock import patch

from django.db import IntegrityError

from apps.projects.models import Task, TaskActivity, TaskLabel, TaskOrder, TaskState, TaskStateDefinition
from . import test_state_templates as fixtures


class TemplateApplicationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.StateTemplateTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.StateTemplateTests.tearDownClass.__func__)
    setUp = fixtures.StateTemplateTests.setUp
    tearDown = fixtures.StateTemplateTests.tearDown
    account = fixtures.StateTemplateTests.account
    request = fixtures.StateTemplateTests.request
    project_payload = fixtures.StateTemplateTests.project_payload
    project = fixtures.StateTemplateTests.project
    assigned_project = fixtures.StateTemplateTests.assigned_project
    task_payload = fixtures.StateTemplateTests.task_payload
    task = fixtures.StateTemplateTests.task
    configuration = fixtures.StateTemplateTests.configuration
    templates = fixtures.StateTemplateTests.templates
    preset = fixtures.StateTemplateTests.preset
    apply = fixtures.StateTemplateTests.apply

    def flow(self, active=3, done=True):
        return [
            {"code": "draft_start", "name": "ABIERTO", "category": "not_started", "tema_id": 6},
            *[{"code": f"draft_active_{index}", "name": f"ACTIVO {index}", "category": "active", "tema_id": 2} for index in range(active)],
            *([{"code": "draft_done", "name": "TERMINADO", "category": "done", "tema_id": 1}] if done else []),
            {"code": "draft_closed", "name": "CERRADO", "category": "closed", "tema_id": 1},
        ]

    def install_flow(self, project, states):
        response = self.apply(project, states)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["states"]

    def values(self, project):
        return {row.pop("id"): row for row in Task.objects.filter(project=project).values(
            "id", "parent_id", "archived", "responsible_id", "name", "description", "priority", "start_date", "due_date", "estimated_minutes", "progress", "reason",
        )}

    def test_positional_mapping_many_to_one_archived_children_and_attributes(self):
        project = self.assigned_project()
        old = self.install_flow(project, self.flow())
        tasks = [self.task(project, state_code=state["code"]) for state in old]
        child = self.task(project, parent_id=tasks[1].pk, state_code=old[3]["code"])
        archived = self.task(project, state_code=old[3]["code"])
        Task.objects.filter(pk=archived.pk).update(archived=True)
        Task.objects.filter(project=project).update(progress=37, reason="Dato histórico", estimated_minutes=95)
        TaskLabel.objects.create(task=child, name="Conservar", kind="technical")
        before, positions = self.values(project), dict(TaskOrder.objects.values_list("task_id", "position"))
        count = TaskActivity.objects.count()
        response = self.apply(project, self.flow(active=2))
        self.assertEqual(response.status_code, 200, response.content)
        new = response.json()["states"]
        active = [state for state in new if state["category"] == "active"]
        for task, target in zip(tasks[1:4], (active[0], active[1], active[1])):
            task.refresh_from_db()
            self.assertEqual(task.state_code, target["code"])
        for task in (child, archived):
            task.refresh_from_db()
            self.assertEqual(task.state_code, active[-1]["code"])
        self.assertEqual(self.values(project), before)
        self.assertEqual(dict(TaskOrder.objects.values_list("task_id", "position")), positions)
        self.assertEqual(TaskLabel.objects.get(task=child).name, "Conservar")
        events = TaskActivity.objects.filter(project=project, action="changed", field="state_code").order_by("id")
        self.assertEqual(TaskActivity.objects.count() - count, len(tasks) + 2)
        latest = list(events)[-(len(tasks) + 2):]
        self.assertEqual(len({event.operation_id for event in latest}), 1)
        self.assertTrue(all(event.actor_id == self.admin.pk and event.old_value["category"] == event.new_value["category"] for event in latest))
        self.assertNotIn(archived.pk, [task["id"] for task in response.json()["tasks"]])
        snapshots = {task["id"]: task for task in response.json()["tasks"]}
        self.assertEqual(snapshots[child.pk]["state_code"], child.state_code)

    def test_same_code_keeps_category_and_does_not_duplicate_remapped_history(self):
        project = self.assigned_project()
        old = self.install_flow(project, self.flow(active=2))
        kept = next(state for state in old if state["category"] == "active")
        tasks = [self.task(project, state_code=state["code"]) for state in old if state["category"] == "active"]
        requested = self.flow(active=1)
        requested[1] = {**kept, "name": "RENOMBRADO", "tema_id": 2}
        new = self.install_flow(project, requested)
        for task in tasks:
            task.refresh_from_db()
            self.assertEqual(task.state_code, kept["code"])
            event = TaskActivity.objects.filter(task=task, field="state_code").get()
            self.assertEqual(event.new_value["name"], "RENOMBRADO")
        count = TaskActivity.objects.count()
        self.install_flow(project, new)
        self.assertEqual(TaskActivity.objects.count(), count)
        changed = [{**state, "category": "closed"} if state["code"] == kept["code"] else state for state in new]
        self.assertEqual(self.apply(project, changed).status_code, 400)

    def test_configured_position_wins_over_ids_names_and_more_destinations(self):
        project = self.assigned_project()
        old = self.install_flow(project, self.flow())
        old = self.install_flow(project, [old[0], old[3], old[1], old[2], *old[4:]])
        task = self.task(project, state_code=old[1]["code"])
        history = list(TaskActivity.objects.values())
        new = self.install_flow(project, self.flow(active=4))
        task.refresh_from_db()
        self.assertEqual(task.state_code, new[1]["code"])
        new = self.install_flow(project, self.flow(active=1))
        task.refresh_from_db()
        self.assertEqual(task.state_code, new[1]["code"])
        new = self.install_flow(project, self.flow(active=3))
        task.refresh_from_db()
        self.assertEqual(task.state_code, new[1]["code"])
        self.assertEqual(list(TaskActivity.objects.filter(pk__in=[event["id"] for event in history]).values()), history)

    def test_used_empty_category_rejected_before_new_codes_and_can_be_corrected(self):
        project = self.assigned_project()
        old = self.install_flow(project, self.flow())
        state = next(state for state in old if state["category"] == "done")
        task = self.task(project, state_code=state["code"])
        configuration, codes, count = self.configuration(project), set(TaskState.objects.values_list("code", flat=True)), TaskActivity.objects.count()
        with patch("apps.projects.services.statuses.TaskState.objects.bulk_create", wraps=TaskState.objects.bulk_create) as create:
            response = self.apply(project, self.flow(done=False))
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn("done", response.json()["categories"])
            create.assert_not_called()
        self.assertEqual(self.configuration(project), configuration)
        self.assertEqual(set(TaskState.objects.values_list("code", flat=True)), codes)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.install_flow(project, self.flow())
        task.refresh_from_db()
        self.assertEqual(TaskStateDefinition.objects.get(code=task.state_code).category, "done")

    def test_empty_project_and_empty_done_category_are_valid(self):
        project = self.project()
        states = self.install_flow(project, self.flow(done=False))
        self.assertNotIn("done", [state["category"] for state in states])
        self.assertEqual(TaskActivity.objects.count(), 0)

    def test_scrum_completed_is_active_and_never_finishes_work(self):
        project = self.assigned_project()
        old = self.install_flow(project, self.preset(project, "scrum"))
        state = next(state for state in old if state["name"] == "COMPLETADO")
        self.assertEqual(state["category"], "active")
        task = self.task(project, state_code=state["code"])
        Task.objects.filter(pk=task.pk).update(progress=42)
        new = self.install_flow(project, self.preset(project, "marketing"))
        active = [state for state in new if state["category"] == "active"]
        task.refresh_from_db()
        self.assertEqual(task.state_code, active[2]["code"])
        self.assertEqual(task.progress, 42)

    def test_conflict_failure_and_foreign_code_leave_no_partial_configuration(self):
        project = self.assigned_project()
        old = self.install_flow(project, self.flow())
        self.task(project, state_code=old[1]["code"])
        before, codes, count = self.configuration(project), set(TaskState.objects.values_list("code", flat=True)), TaskActivity.objects.count()
        response = self.request("patch", f"{project.pk}/task-states/", {"template": "custom", "revision": "0" * 64, "states": self.flow()}, self.admin)
        self.assertEqual(response.status_code, 409)
        from apps.projects.services import statuses
        original = statuses.reassign_tasks
        def fail_after_writes(*args):
            original(*args)
            raise IntegrityError("Synthetic failure after reassignment")
        with patch("apps.projects.services.statuses.reassign_tasks", side_effect=fail_after_writes):
            self.assertEqual(self.apply(project, self.flow(active=1)).status_code, 409)
        self.assertEqual(self.configuration(project), before)
        self.assertEqual(set(TaskState.objects.values_list("code", flat=True)), codes)
        self.assertEqual(TaskActivity.objects.count(), count)
        other = self.project()
        self.assertEqual(self.apply(other, old).status_code, 400)
        self.assertEqual(self.request("patch", f"{project.pk}/task-states/", {"template": "custom", "revision": before["revision"], "states": self.flow()}, self.worker).status_code, 403)
