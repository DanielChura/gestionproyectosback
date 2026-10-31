from datetime import timedelta
from uuid import uuid4
import unittest
from unittest.mock import patch

from apps.organization.models import Specialty
from apps.projects.models import Project, ProjectMember, ProjectTeam, ProjectTaskStatus, Task, TaskActivity, TaskLabel, TaskOrder
from apps.projects.services.state_templates import preset_templates
from apps.teams.models import Team, TeamMember
from apps.workers.models import WorkerArea
from . import test_projects as fixtures


class ProjectIntegrationTests(unittest.TestCase):
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

    def team(self, name, people, area=None):
        team = Team.objects.create(name=name, area=area or self.area, active=True)
        TeamMember.objects.bulk_create([TeamMember(team=team, worker_id=person.worker_id, joined_date=self.today) for person in people])
        return team

    def snapshot(self, project, task):
        result = self.request("get", f"{project.pk}/tasks/{task.pk}/", user=self.admin)
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()["task"]

    def move(self, project, task, target, before=None, revision=None, user=None, reason=""):
        return self.request("post", f"{project.pk}/tasks/{task.pk}/move/", {
            "state_code": target, "before_id": before, "reason": reason,
            "expected_revision": revision or self.snapshot(project, task)["move_revision"],
        }, user or self.admin)

    def test_team_snapshot_deduplicates_and_never_grants_future_membership(self):
        second = self.account("second", "TRABAJADOR", self.area)
        one, two = self.team("Equipo A", [self.worker]), self.team("Equipo B", [self.worker, second])
        response = self.request("post", data=self.project_payload(mode="assigned", participant_source="teams", team_ids=[one.pk, two.pk]), user=self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        project = Project.objects.get(pk=response.json()["id"])
        self.assertEqual(ProjectTeam.objects.filter(project=project).count(), 2)
        self.assertEqual(set(ProjectMember.objects.filter(project=project).values_list("worker_id", flat=True)), {self.worker.worker_id, second.worker_id})
        self.assertEqual(set(ProjectMember.objects.filter(project=project).values_list("project_role", flat=True)), {"COLABORADOR"})
        late = self.account("late", "TRABAJADOR", self.area)
        TeamMember.objects.create(team=one, worker=late.worker, joined_date=self.today)
        self.assertEqual(self.request("get", f"{project.pk}/", user=late).status_code, 404)
        ProjectMember.objects.filter(project=project, worker=self.worker.worker).update(active=False)
        self.assertEqual(self.request("get", f"{project.pk}/", user=self.worker).status_code, 404)

    def test_team_area_members_and_active_accounts_are_revalidated_without_partial_project(self):
        invalid = self.team("Otra área", [self.outsider], self.other_area)
        empty = self.team("Vacío", [])
        for team in (invalid, empty):
            response = self.request("post", data=self.project_payload(mode="assigned", participant_source="teams", team_ids=[team.pk]), user=self.admin)
            self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Project.objects.exists())
        team = self.team("Elegible", [self.worker])
        self.worker.worker.active = False
        self.worker.worker.save()
        result = self.request("post", data=self.project_payload(mode="assigned", participant_source="teams", team_ids=[team.pk]), user=self.admin)
        self.assertEqual(result.status_code, 400, result.content)
        self.assertFalse(Project.objects.exists())

    def test_worker_area_associations_override_historical_primary_area(self):
        WorkerArea.objects.create(worker=self.outsider.worker, area=self.area)
        WorkerArea.objects.create(worker=self.worker.worker, area=self.other_area)
        team = self.team("Asociaciones", [self.worker, self.outsider])
        options = self.request("get", f"teams/?area_id={self.area.pk}", user=self.admin).json()
        self.assertEqual(options[0]["eligible_count"], 1)
        self.assertEqual(options[0]["members"][0]["id"], self.outsider.worker_id)
        result = self.request("get", f"workers/?area_ids={self.area.pk}", user=self.admin)
        self.assertIn(self.outsider.worker_id, [item["id"] for item in result.json()])
        self.assertEqual(team.area_id, self.area.pk)

    def test_hidden_source_selections_and_duplicates_are_rejected(self):
        team = self.team("Equipo", [self.worker])
        for changes in (
            {"mode": "assigned", "participant_source": "teams", "team_ids": [team.pk], "worker_ids": [self.worker.worker_id]},
            {"mode": "assigned", "participant_source": "teams", "team_ids": [team.pk, team.pk]},
            {"mode": "available", "participant_source": "teams", "team_ids": [team.pk]},
        ):
            self.assertEqual(self.request("post", data=self.project_payload(**changes), user=self.admin).status_code, 400)
        self.assertFalse(Project.objects.exists())

    def test_initial_scrum_flow_is_atomic_and_completed_remains_active(self):
        preset = next(item for item in preset_templates() if item["id"] == "scrum")
        result = self.request("post", data=self.project_payload(initial_task_states=preset["states"]), user=self.admin)
        self.assertEqual(result.status_code, 201, result.content)
        states = result.json()["task_states"]["states"]
        self.assertEqual(next(item for item in states if item["name"] == "COMPLETADO")["category"], "active")
        self.assertTrue(all(item["code"].startswith("S_") for item in states))
        self.assertEqual(next(item for item in preset_templates() if item["id"] == "scrum"), preset)

    def test_bad_flow_rolls_back_project_members_teams_and_new_states(self):
        team = self.team("Equipo", [self.worker])
        response = self.request("post", data=self.project_payload(mode="assigned", participant_source="teams", team_ids=[team.pk],
            initial_task_states=[{"name": "Solo abierto", "category": "not_started", "color": "#737373"}]), user=self.admin)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Project.objects.exists())
        self.assertFalse(ProjectMember.objects.exists())
        self.assertFalse(ProjectTeam.objects.exists())
        self.assertFalse(ProjectTaskStatus.objects.exists())

    def test_specialty_identity_history_retention_removal_and_noop(self):
        project = self.assigned_project()
        one = Specialty.objects.create(name="Diseño")
        two = Specialty.objects.create(name="Backend")
        task = self.task(project, specialty_ids=[one.pk, two.pk])
        original = TaskLabel.objects.filter(task=task, specialty=one).get().pk
        legacy = TaskLabel.objects.create(task=task, name="Histórica sin correspondencia", kind="nontechnical")
        path = f"{project.pk}/tasks/{task.pk}/"
        before = TaskActivity.objects.count()
        response = self.request("patch", path, {"specialty_ids": [one.pk, two.pk], "retained_label_ids": [legacy.pk]}, self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(TaskActivity.objects.count(), before)
        one.name = "Diseño actualizado"
        one.save()
        self.assertIn("Diseño actualizado", [item["name"] for item in self.snapshot(project, task)["labels"]])
        self.assertEqual(TaskLabel.objects.get(pk=original).specialty_id, one.pk)
        response = self.request("patch", path, {"specialty_ids": [one.pk], "retained_label_ids": []}, self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(TaskLabel.objects.filter(task=task).count(), 1)
        self.assertTrue(TaskActivity.objects.filter(task=task, field="labels").exists())

    def test_bad_specialties_and_foreign_historical_labels_roll_back(self):
        project = self.assigned_project()
        one = self.task(project)
        two = self.task(project)
        foreign = TaskLabel.objects.create(task=two, name="Ajena", kind="technical")
        count = TaskActivity.objects.count()
        for data in ({"specialty_ids": [999999]}, {"retained_label_ids": [foreign.pk]}, {"specialty_ids": [1, 1]}):
            result = self.request("patch", f"{project.pk}/tasks/{one.pk}/", {"name": "No guardar", **data}, self.admin)
            self.assertEqual(result.status_code, 400, result.content)
            one.refresh_from_db()
            self.assertNotEqual(one.name, "No guardar")
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_assignment_teams_do_not_enroll_new_project_members(self):
        project = self.assigned_project()
        second = self.account("second", "TRABAJADOR", self.area)
        self.team("Buscar personas", [self.worker, second])
        result = self.request("get", f"{project.pk}/assignment-teams/", user=self.admin)
        self.assertEqual(result.status_code, 200, result.content)
        people = {person["id"]: person for person in result.json()[0]["members"]}
        self.assertFalse(people[second.worker_id]["eligible"])
        task = self.task(project)
        denied = self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"responsible_ids": [second.worker_id]}, self.admin)
        self.assertEqual(denied.status_code, 400)
        self.assertFalse(ProjectMember.objects.filter(project=project, worker=second.worker).exists())

    def test_move_state_and_order_preserve_attributes_and_detect_concurrency(self):
        project = self.assigned_project()
        one = self.task(project, responsible_id=self.worker.worker_id)
        two = self.task(project, state_code="EN_CURSO", responsible_id=self.worker.worker_id)
        before = self.snapshot(project, one)
        revision = before["move_revision"]
        result = self.move(project, one, "EN_CURSO", two.pk, revision)
        self.assertEqual(result.status_code, 200, result.content)
        one.refresh_from_db()
        self.assertEqual(one.state_code, "EN_CURSO")
        self.assertLess(TaskOrder.objects.get(task=one).position, TaskOrder.objects.get(task=two).position)
        after = self.snapshot(project, one)
        for field in ("parent_id", "name", "priority", "description", "labels", "start_date", "due_date", "assignees"):
            self.assertEqual(before[field], after[field], field)
        count = TaskActivity.objects.count()
        conflict = self.move(project, one, "EN_CURSO", revision=revision)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_move_reason_permission_dependencies_hierarchy_and_atomic_rollback(self):
        project = self.assigned_project()
        one = self.task(project, responsible_id=self.worker.worker_id)
        two = self.task(project)
        self.assertEqual(self.move(project, one, "EN_CURSO", user=self.worker).status_code, 403)
        other = self.task(self.assigned_project())
        self.assertEqual(self.move(project, one, "EN_CURSO", other.pk).status_code, 400)
        child = self.task(project, parent_id=two.pk, responsible_id=self.worker.worker_id)
        self.assertEqual(self.move(project, child, "EN_CURSO", one.pk).status_code, 400)
        two.state_code = "EN_CURSO"
        two.responsible_id = self.worker.worker_id
        two.save()
        count = TaskActivity.objects.count()
        positions = dict(TaskOrder.objects.values_list("task_id", "position"))
        with patch("apps.projects.services.ordering.record_activity", side_effect=RuntimeError("controlled")):
            with self.assertRaises(RuntimeError):
                self.move(project, one, "EN_CURSO")
        one.refresh_from_db()
        self.assertEqual(one.state_code, "PENDIENTE")
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(dict(TaskOrder.objects.values_list("task_id", "position")), positions)

    def test_permitted_past_dates_preserve_bounds_and_activity(self):
        project = self.assigned_project()
        project.start_date = self.today - timedelta(days=20)
        project.save()
        for values in ({"start_date": str(self.today - timedelta(days=10))}, {"due_date": str(self.today - timedelta(days=2))}):
            result = self.request("post", f"{project.pk}/tasks/", self.task_payload(**values), self.admin)
            self.assertEqual(result.status_code, 400, result.content)
        self.assertFalse(Task.objects.exists())
        task = self.task(project)
        Task.objects.filter(pk=task.pk).update(start_date=self.today - timedelta(days=10), due_date=self.today - timedelta(days=2))
        task.refresh_from_db()
        self.assertEqual(task.start_date, self.today - timedelta(days=10))
        self.assertEqual(self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"priority": 2}, self.admin).status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.start_date, self.today - timedelta(days=10))
        response = self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"due_date": str(self.today - timedelta(days=1)), "expected_calendar_revision": self.snapshot(project, task)["calendar_revision"], "calendar_intent_id": str(uuid4())}, self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        invalid = self.request("patch", f"{project.pk}/tasks/{task.pk}/", {"start_date": str(self.today - timedelta(days=21)), "expected_calendar_revision": self.snapshot(project, task)["calendar_revision"], "calendar_intent_id": str(uuid4())}, self.admin)
        self.assertEqual(invalid.status_code, 400)

    def test_manual_and_direct_move_reach_enabled_nonsequential_states_without_reason(self):
        project = self.assigned_project()
        task = self.task(project, responsible_id=self.worker.worker_id, labels=[{"name": "Preservada", "kind": "technical"}])
        Task.objects.filter(pk=task.pk).update(reason="Motivo histórico")
        before = self.snapshot(project, task)
        normal = self.request("post", f"{project.pk}/tasks/{task.pk}/state/", {"state_code": "COMPLETADA"}, self.admin)
        self.assertEqual(normal.status_code, 200, normal.content)
        self.assertEqual(TaskActivity.objects.filter(task=task, field="state_code").latest("pk").actor_id, self.admin.pk)
        for state in ("BLOQUEADA", "CANCELADA", "OBSERVADA", "PENDIENTE", "EN_CURSO", "COMPLETADA"):
            self.assertIn(state, self.snapshot(project, task)["move_targets"])
            count = TaskActivity.objects.filter(task=task, field="state_code").count()
            result = self.move(project, task, state)
            self.assertEqual(result.status_code, 200, result.content)
            task.refresh_from_db()
            self.assertEqual(task.state_code, state)
            self.assertEqual(task.reason, "Motivo histórico")
            after = self.snapshot(project, task)
            for field in ("parent_id", "name", "priority", "description", "labels", "start_date", "due_date", "assignees"):
                self.assertEqual(before[field], after[field], field)
            events = TaskActivity.objects.filter(task=task, field="state_code")
            self.assertEqual(events.count(), count + 1)
            self.assertEqual(events.latest("pk").actor_id, self.admin.pk)
        count = TaskActivity.objects.count()
        denied = self.move(project, task, "DESCONOCIDO")
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_move_keeps_dependencies_independent_subtasks_and_noop_history(self):
        project = self.assigned_project()
        predecessor = self.task(project, responsible_id=self.worker.worker_id)
        task = self.task(project, responsible_id=self.worker.worker_id)
        self.assertEqual(self.request("post", f"{project.pk}/tasks/{task.pk}/dependencies/", {"predecessor_id": predecessor.pk}, self.admin).status_code, 201)
        self.assertIn("EN_CURSO", self.snapshot(project, task)["move_targets"])
        count = TaskActivity.objects.filter(task=task, field="state_code").count()
        self.assertEqual(self.move(project, task, "EN_CURSO").status_code, 200)
        self.assertEqual(TaskActivity.objects.filter(task=task, field="state_code").count(), count + 1)
        predecessor.refresh_from_db()
        self.assertEqual(predecessor.state_code, "PENDIENTE")
        self.assertEqual(list(task.incoming_dependencies.values_list("predecessor_id", flat=True)), [predecessor.pk])
        parent = self.task(project, state_code="EN_REVISION", responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk, responsible_id=self.worker.worker_id)
        self.assertIn("COMPLETADA", self.snapshot(project, parent)["move_targets"])
        self.assertEqual(self.move(project, parent, "COMPLETADA").status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.state_code, "PENDIENTE")
        self.assertEqual(self.move(project, child, "EN_CURSO").status_code, 200)
        child.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual((child.parent_id, child.state_code, parent.state_code), (parent.pk, "EN_CURSO", "COMPLETADA"))
        count = TaskActivity.objects.count()
        position = TaskOrder.objects.get(task=child).position
        self.assertEqual(self.move(project, child, "EN_CURSO").status_code, 200)
        self.assertEqual(TaskActivity.objects.count(), count)
        self.assertEqual(TaskOrder.objects.get(task=child).position, position)
        invalid = self.move(project, child, "BLOQUEADA", task.pk)
        self.assertEqual(invalid.status_code, 400)
        child.refresh_from_db()
        self.assertEqual((child.parent_id, child.state_code), (parent.pk, "EN_CURSO"))
        self.assertEqual(TaskActivity.objects.count(), count)

    def test_same_state_manual_order_is_stable_and_backfill_preserves_legacy(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        project = self.assigned_project()
        legacy = self.team("Equipo histórico", [self.worker])
        project.team = legacy
        project.save(update_fields=["team"])
        first, second = self.task(project), self.task(project)
        label = TaskLabel.objects.create(task=first, name="Sin correspondencia", kind="technical")
        members = list(ProjectMember.objects.filter(project=project).values_list("worker_id", "active"))
        TaskOrder.objects.all().delete()
        with connection.schema_editor() as editor:
            import_module("apps.projects.migrations.0013_project_teams_specialties_order").fill(apps, editor)
        self.assertEqual(ProjectTeam.objects.get(project=project).team_id, legacy.pk)
        self.assertEqual(list(ProjectMember.objects.filter(project=project).values_list("worker_id", "active")), members)
        label.refresh_from_db()
        self.assertIsNone(label.specialty_id)
        self.assertEqual(label.kind, "technical")
        self.assertEqual(self.move(project, second, "PENDIENTE", first.pk).status_code, 200)
        listing = self.request("get", f"{project.pk}/tasks/", user=self.admin).json()
        self.assertEqual([item["id"] for item in listing], [second.pk, first.pk])
        count = TaskActivity.objects.count()
        self.assertEqual(self.move(project, second, "PENDIENTE", first.pk).status_code, 200)
        self.assertEqual(TaskActivity.objects.count(), count)
