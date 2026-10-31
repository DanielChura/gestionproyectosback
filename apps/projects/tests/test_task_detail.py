import unittest
from uuid import uuid4
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test.utils import CaptureQueriesContext
from django.db import connection

from apps.projects.models import ProjectMember, ProjectTaskStatus, Task, TaskActivity, TaskRelation
from . import test_projects as fixtures


class TaskDetailTests(unittest.TestCase):
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

    def history(self, project, task, cursor=None, user=None):
        response = self.request("get", f"{project.pk}/tasks/{task.pk}/activity/" + (f"?cursor={cursor}" if cursor else ""), user=user or self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_detail_estimate_and_partial_edit_persist_with_atomic_activity(self):
        project = self.assigned_project(worker_edit=True)
        task = self.task(project, responsible_id=self.worker.worker_id)
        before = self.history(project, task)
        path = f"{project.pk}/tasks/{task.pk}/"
        response = self.request("patch", path, {"name": "  Renombrada  ", "estimated_minutes": 95}, self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["name"], "Renombrada")
        self.assertEqual(response.json()["estimated_minutes"], 95)
        self.assertEqual(response.json()["responsible_id"], self.worker.worker_id)
        detail = self.request("get", path, user=self.admin).json()
        self.assertEqual(detail["task"]["estimated_minutes"], 95)
        self.assertIsNone(detail["parent"])
        after = self.history(project, task)
        self.assertEqual(len(after["items"]), len(before["items"]) + 1)
        events = after["items"][-1]["events"]
        self.assertEqual({event["field"] for event in events}, {"name", "estimated_minutes"})
        self.assertEqual({event["actor_id"] for event in events}, {self.admin.pk})
        self.assertTrue(all(event["created_at"] for event in events))
        self.assertEqual(self.request("patch", path, {"estimated_minutes": None}, self.admin).status_code, 200)
        task.refresh_from_db()
        self.assertIsNone(task.estimated_minutes)

    def test_noop_invalid_and_rollback_never_create_activity(self):
        project = self.project()
        task = self.task(project)
        total = TaskActivity.objects.count()
        path = f"{project.pk}/tasks/{task.pk}/"
        self.assertEqual(self.request("patch", path, {"name": task.name}, self.admin).status_code, 200)
        for payload in ({"name": " "}, {"estimated_minutes": 0}, {"estimated_minutes": -1},
                        {"estimated_minutes": "1.5"}, {"actor_id": 1}, {"estimated_minutes": 2147483648}):
            self.assertEqual(self.request("patch", path, payload, self.admin).status_code, 400)
        with patch("apps.projects.services.tasks.save_labels", side_effect=IntegrityError("synthetic")):
            response = self.request("patch", path, {"name": "No persistir", "estimated_minutes": 40}, self.admin)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(TaskActivity.objects.count(), total)
        task.refresh_from_db()
        self.assertNotEqual(task.name, "No persistir")
        self.assertIsNone(task.estimated_minutes)

    def test_child_creation_visible_in_parent_and_parent_navigation_is_authorized(self):
        project = self.assigned_project()
        parent = self.task(project, responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk, responsible_id=self.worker.worker_id)
        parent_events = self.history(project, parent)["items"][-1]["events"]
        self.assertEqual(parent_events[0]["action"], "child_created")
        self.assertEqual(parent_events[0]["related_task_id"], child.pk)
        response = self.request("get", f"{project.pk}/tasks/{child.pk}/", user=self.worker)
        self.assertEqual(response.json()["parent"]["id"], parent.pk)
        parent.responsible_id = None
        parent.save()
        self.assertIsNone(self.request("get", f"{project.pk}/tasks/{child.pk}/", user=self.worker).json()["parent"])

    def test_global_subtask_counts_are_not_page_or_label_based(self):
        project = self.project()
        parent = self.task(project)
        ProjectTaskStatus.objects.bulk_create([
            ProjectTaskStatus(project=project, code="PENDIENTE", name="COMPLETADO", category="active", position=0),
            ProjectTaskStatus(project=project, code="COMPLETADA", name="Final", category="closed", position=1),
        ])
        for index in range(35):
            Task.objects.create(project=project, parent=parent, name=str(index), state_code="PENDIENTE" if index < 32 else "COMPLETADA",
                                start_date=project.start_date, due_date=None)
        response = self.request("get", f"{project.pk}/tasks/{parent.pk}/subtasks/", user=self.admin).json()
        self.assertEqual((response["total"], response["completed"], len(response["items"])), (35, 3, 30))
        following = self.request("get", f"{project.pk}/tasks/{parent.pk}/subtasks/?cursor={response['next_cursor']}", user=self.admin).json()
        self.assertEqual((following["total"], following["completed"], len(following["items"])), (35, 3, 5))

    def test_relations_are_symmetric_informational_unique_and_removable(self):
        project = self.project()
        first, second = self.task(project, name="A"), self.task(project, name="B")
        path = f"{project.pk}/tasks/{first.pk}/relations/"
        self.assertEqual(self.request("post", path, {"name": "  Referencia  ", "target_id": second.pk}, self.admin).status_code, 201)
        self.assertEqual(self.request("post", f"{project.pk}/tasks/{second.pk}/relations/", {"name": "referencia", "target_id": first.pk}, self.admin).status_code, 400)
        self.assertEqual(TaskRelation.objects.count(), 1)
        row = self.request("get", path, user=self.admin).json()["items"][0]
        self.assertEqual((row["kind"], row["target_id"]), ("custom", second.pk))
        first.refresh_from_db()
        self.assertEqual(first.state_code, "PENDIENTE")
        self.assertEqual(self.request("delete", path + f"{row['relation_id']}/", user=self.admin).status_code, 204)
        self.assertFalse(TaskRelation.objects.exists())
        self.assertEqual(self.history(project, second)["items"][-1]["events"][0]["action"], "relation_removed")

    def test_search_is_bounded_authorized_and_dependencies_have_one_direction(self):
        project = self.project()
        first, second = self.task(project, name="Primera"), self.task(project, name="Segunda")
        child = self.task(project, parent_id=first.pk)
        other = self.project()
        self.task(other, name="No filtrar")
        root = f"{project.pk}/tasks/{first.pk}/"
        rows = self.request("get", root + "related-candidates/?kind=blocks&q=Segunda", user=self.admin).json()["items"]
        self.assertEqual([row["id"] for row in rows], [second.pk])
        self.assertEqual(self.request("get", f"{project.pk}/tasks/{child.pk}/related-candidates/?kind=blocks", user=self.admin).status_code, 400)
        self.assertEqual(self.request("post", f"{project.pk}/tasks/{second.pk}/dependencies/", {"predecessor_id": first.pk}, self.admin).status_code, 201)
        self.assertEqual(self.request("get", root + "relations/", user=self.admin).json()["items"][0]["kind"], "blocks")
        self.assertEqual(self.request("get", f"{project.pk}/tasks/{second.pk}/relations/", user=self.admin).json()["items"][0]["kind"], "blocked_by")
        self.assertEqual(self.request("post", root + "dependencies/", {"predecessor_id": second.pk}, self.admin).status_code, 400)

    def test_permissions_revocation_and_history_immutability(self):
        project = self.assigned_project(worker_edit=False)
        task = self.task(project, responsible_id=self.worker.worker_id)
        root = f"{project.pk}/tasks/{task.pk}/"
        self.assertEqual(self.request("get", root, user=self.worker).status_code, 200)
        self.assertEqual(self.request("get", root + "activity/", user=self.worker).status_code, 200)
        self.assertEqual(self.request("patch", root, {"estimated_minutes": 20}, self.worker).status_code, 403)
        self.assertEqual(self.request("post", root + "relations/", {"name": "X", "target_id": task.pk}, self.worker).status_code, 403)
        for method in ("post", "patch", "delete"):
            self.assertEqual(self.request(method, root + "activity/", {}, self.admin).status_code, 405)
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=False)
        for suffix in ("", "activity/", "subtasks/", "relations/", "related-candidates/"):
            self.assertIn(self.request("get", root + suffix, user=self.worker).status_code, (403, 404))

    def test_activity_pagination_keeps_complete_operations_and_historical_names(self):
        project = self.project()
        task = self.task(project)
        original_name = self.admin.worker.first_names
        for index in range(24):
            self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"name": f"Nombre {index}", "estimated_minutes": index + 1}, self.admin)
        first = self.history(project, task)
        second = self.history(project, task, first["next_cursor"])
        ids = [group["id"] for group in first["items"] + second["items"]]
        self.assertEqual(len(ids), 25)
        self.assertEqual(len(set(ids)), 25)
        ordered = sorted(first["items"] + second["items"], key=lambda group: group["events"][0]["id"])
        self.assertEqual(ordered[0]["events"][0]["action"], "created")
        self.assertTrue(all(len(group["events"]) == 2 for group in ordered[1:]))
        self.admin.worker.first_names = "Otro nombre"
        self.admin.worker.save(update_fields=["first_names"])
        self.assertIn(original_name, self.history(project, task)["items"][0]["events"][0]["actor_name"])

    def test_legacy_tasks_have_no_fabricated_events(self):
        project = self.project()
        task = Task.objects.create(project=project, name="Anterior", state_code="PENDIENTE", start_date=project.start_date)
        self.assertEqual(self.history(project, task)["items"], [])

    def test_assignment_labels_dates_and_priority_record_stable_values_in_one_operation(self):
        project = self.assigned_project(worker_edit=True)
        task = self.task(project, responsible_id=self.worker.worker_id)
        response = self.request("patch", f"{project.pk}/tasks/{task.pk}/", {
            "expected_calendar_revision": str(task.calendar_revision), "calendar_intent_id": str(uuid4()),
            "responsible_ids": [], "labels": [{"name": "Cálculo QA", "kind": "technical"}],
            "start_date": str(self.today + timedelta(days=1)), "due_date": None, "priority": None,
        }, self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        events = self.history(project, task)["items"][-1]["events"]
        self.assertEqual({event["field"] for event in events}, {"responsibles", "labels", "start_date", "due_date", "priority"})
        assignment = next(event for event in events if event["field"] == "responsibles")
        self.assertEqual(assignment["old_value"][0]["id"], self.worker.worker_id)
        self.assertEqual(assignment["new_value"], [])
        self.assertEqual(len({event["operation_id"] for event in events}), 1)

    def test_related_task_visibility_filters_links_search_counts_and_activity(self):
        project = self.assigned_project()
        parent = self.task(project, responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk)
        other = self.task(project)
        self.assertEqual(self.request("post", f"{project.pk}/tasks/{parent.pk}/relations/", {"name": "Privada", "target_id": other.pk}, self.admin).status_code, 201)
        self.assertFalse(self.request("get", f"{project.pk}/tasks/{parent.pk}/relations/", user=self.worker).json()["items"])
        self.assertEqual(self.request("get", f"{project.pk}/tasks/{parent.pk}/subtasks/", user=self.worker).json()["total"], 0)
        events = [event for group in self.history(project, parent, user=self.worker)["items"] for event in group["events"]]
        self.assertFalse(any(event["related_task_id"] in (child.pk, other.pk) for event in events))
        self.assertEqual(self.request("get", f"{project.pk}/tasks/{parent.pk}/related-candidates/", user=self.worker).status_code, 403)

    def test_invalid_relation_is_atomic_and_candidate_cursor_excludes_cycles_and_duplicates(self):
        project = self.project()
        first, second = self.task(project), self.task(project)
        foreign = self.task(self.project())
        root = f"{project.pk}/tasks/{first.pk}/"
        count = TaskActivity.objects.count()
        for target in (first.pk, foreign.pk):
            self.assertIn(self.request("post", root + "relations/", {"name": "No persistir", "target_id": target}, self.admin).status_code, (400, 404))
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertFalse(TaskRelation.objects.exists())
        self.assertEqual(self.request("post", f"{project.pk}/tasks/{second.pk}/dependencies/", {"predecessor_id": first.pk}, self.admin).status_code, 201)
        self.assertFalse(self.request("get", root + "related-candidates/?kind=blocks", user=self.admin).json()["items"])
        self.assertFalse(self.request("get", root + "related-candidates/?kind=blocked_by", user=self.admin).json()["items"])
        for cursor in ("bad", "-1", "9223372036854775808"):
            self.assertEqual(self.request("get", root + f"activity/?cursor={cursor}", user=self.admin).status_code, 400)

    def test_detail_collections_have_bounded_pages_and_constant_queries(self):
        project = self.project()
        parent = self.task(project)
        counts = {suffix: [] for suffix in ("subtasks/", "relations/", "related-candidates/?kind=custom", "activity/")}
        previous_size = 0
        for size in (1, 60):
            for index in range(previous_size, size):
                child = Task.objects.create(project=project, parent=parent, name=f"Hija {index}", state_code="PENDIENTE", start_date=parent.start_date)
                TaskRelation.objects.create(project=project, left_task=parent, right_task=child, name="Referencia", name_key="referencia")
                TaskActivity.objects.create(project=project, task=parent, operation_id=f"synthetic-{index}", actor_id=self.admin.pk,
                                            actor_name="Autor sintético", action="child_created", new_value={"name": child.name}, related_task_id=child.pk)
            previous_size = size
            for suffix, queries in counts.items():
                with CaptureQueriesContext(connection) as captured:
                    response = self.request("get", f"{project.pk}/tasks/{parent.pk}/{suffix}", user=self.admin)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertLessEqual(len(response.json()["items"]), 20 if suffix == "activity/" else 30)
                queries.append(len(captured))
        for queries in counts.values():
            self.assertEqual(queries[0], queries[1])
            self.assertLessEqual(max(queries), 30)
