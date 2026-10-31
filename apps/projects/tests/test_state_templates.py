from unittest.mock import patch
import unittest

from django.db import IntegrityError

from apps.projects.models import ProjectTaskStatus, TaskState, TaskStateDefinition, TaskStatusTemplate, TaskTransition
from . import test_task_states as state_fixtures
from . import test_projects as fixtures


class StateTemplateTests(unittest.TestCase):
    account = fixtures.ProjectFlowTests.account
    request = fixtures.ProjectFlowTests.request
    project_payload = fixtures.ProjectFlowTests.project_payload
    project = fixtures.ProjectFlowTests.project
    assigned_project = fixtures.ProjectFlowTests.assigned_project
    task_payload = fixtures.ProjectFlowTests.task_payload
    task = fixtures.ProjectFlowTests.task
    setUp = fixtures.ProjectFlowTests.setUp
    tearDown = fixtures.ProjectFlowTests.tearDown
    configuration = state_fixtures.TaskStateFlowTests.configuration

    @classmethod
    def setUpClass(cls):
        fixtures.ProjectFlowTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.ProjectFlowTests.tearDownClass.__func__(cls)

    def templates(self, project):
        result = self.request("get", f"{project.pk}/state-templates/", user=self.admin)
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()

    def preset(self, project, name="normal"):
        template = next(item for item in self.templates(project) if item["id"] == name)
        return [{**state, "code": f"draft_{index}"} for index, state in enumerate(template["states"])]

    def apply(self, project, states, reassignments=None):
        return self.request("patch", f"{project.pk}/task-states/", {
            "template": "custom", "revision": self.configuration(project)["revision"],
            "states": states, "reassignments": reassignments or [],
        }, self.admin)

    def test_exact_presets_empty_done_and_scrum_active_completed(self):
        project = self.project()
        items = self.templates(project)
        self.assertEqual([item["name"] for item in items], ["Normal", "Kanban", "Marketing", "Contenido", "Scrum"])
        expected = {
            "normal": ["EN PROGRESO"], "kanban": ["EN PROGRESO", "EN REVISIÓN"],
            "marketing": ["CONCEPTO", "EN PROGRESO", "EN EJECUCIÓN", "EN REVISIÓN"],
            "contenido": ["LISTO", "REDACTANDO", "APROBACIÓN", "RECHAZADO", "PUBLICADO"],
            "scrum": ["PENDIENTE", "EN PROGRESO", "COMPLETADO", "EN REVISIÓN", "ACEPTADO", "RECHAZADO", "BLOQUEADO"],
        }
        for item in items:
            self.assertEqual([state["name"] for state in item["states"] if state["category"] == "active"], expected[item["id"]])
            self.assertFalse(any(state["category"] == "done" for state in item["states"]))
            self.assertEqual(item["states"][0]["name"], "ABIERTO")
            self.assertEqual(item["states"][-1]["name"], "CERRADO")
            response = self.apply(project, self.preset(project, item["id"]))
            self.assertEqual(response.status_code, 200, response.content)

    def test_template_save_update_copy_persistence_without_applying(self):
        project = self.project()
        before = self.configuration(project)
        path = f"{project.pk}/state-templates/"
        payload = {"name": "Plantilla sintética", "states": [{key: value for key, value in state.items() if key != "code"} for state in self.preset(project)]}
        result = self.request("post", path, payload, self.admin)
        self.assertEqual(result.status_code, 201, result.content)
        saved = result.json()
        self.assertEqual(self.configuration(project), before)
        self.assertEqual(self.templates(project)[-1], saved)
        updated = self.request("put", f"{path}{saved['id']}/", {**payload, "revision": saved["revision"], "states": []}, self.admin)
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.json()["states"], [])
        self.assertEqual(self.request("put", f"{path}{saved['id']}/", {**payload, "revision": 1}, self.admin).status_code, 400)
        self.assertEqual(self.request("post", path, {**payload, "name": "Copia"}, self.admin).status_code, 201)
        self.assertEqual(TaskStatusTemplate.objects.count(), 2)
        self.assertFalse(ProjectTaskStatus.objects.exists())

    def test_template_validation_authentication_scope_and_preset_immutability(self):
        project = self.assigned_project()
        other = self.project()
        path = f"{project.pk}/state-templates/"
        for name in ("", " ", "x" * 71, "Normal", "scrum"):
            self.assertEqual(self.request("post", path, {"name": name, "states": []}, self.admin).status_code, 400)
        saved = self.request("post", path, {"name": "Privada", "states": []}, self.admin).json()
        self.assertEqual(self.request("post", path, {"name": "privada", "states": []}, self.admin).status_code, 400)
        self.assertEqual(len(self.templates(other)), 5)
        self.assertEqual(self.request("put", f"{other.pk}/state-templates/{saved['id']}/", {"name": "Ataque", "states": [], "revision": 1}, self.admin).status_code, 404)
        self.assertEqual(self.request("get", path, user=self.worker).status_code, 403)
        self.assertEqual(self.request("post", path, {"name": "Prohibida", "states": []}, self.worker).status_code, 403)
        self.assertEqual(self.request("put", f"{path}normal/", {"name": "No", "states": []}, self.admin).status_code, 404)

    def test_used_state_maps_automatically_and_keeps_subtasks_assignments(self):
        project = self.assigned_project()
        parent = self.task(project, responsible_id=self.worker.worker_id)
        child = self.task(project, parent_id=parent.pk, responsible_id=self.worker.worker_id)
        states = self.preset(project)
        response = self.apply(project, states)
        self.assertEqual(response.status_code, 200, response.content)
        parent.refresh_from_db(); child.refresh_from_db()
        self.assertEqual(parent.state_code, child.state_code)
        self.assertEqual(child.parent_id, parent.pk)
        self.assertEqual(parent.responsible_id, self.worker.worker_id)
        self.assertFalse(parent.archived)
        self.assertEqual(len(self.templates(project)), 5)

    def test_configuration_failure_rolls_back_new_catalog_and_reassignment(self):
        project = self.assigned_project()
        task = self.task(project, responsible_id=self.worker.worker_id)
        before = self.configuration(project)
        codes = list(TaskState.objects.values_list("code", flat=True))
        with patch("apps.projects.services.statuses.reassign_tasks", side_effect=IntegrityError("synthetic")):
            response = self.apply(project, self.preset(project))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.configuration(project), before)
        self.assertEqual(list(TaskState.objects.values_list("code", flat=True)), codes)
        task.refresh_from_db()
        self.assertEqual(task.state_code, "PENDIENTE")

    def test_identity_category_color_and_empty_states_survive_edits(self):
        project = self.project()
        result = self.apply(project, self.preset(project)).json()
        codes = [item["code"] for item in result["states"]]
        states = [{**item, "name": "RENOMBRADO", "color": "#9333ea"} if index == 0 else item for index, item in enumerate(result["states"])]
        response = self.apply(project, states)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["code"] for item in response.json()["states"]], codes)
        self.assertEqual(response.json()["states"][0]["color"], "#9333ea")
        states[0]["category"] = "active"
        self.assertEqual(self.apply(project, states).status_code, 400)
        self.assertEqual(len(self.configuration(project)["states"]), 3)

    def test_scrum_completed_is_active_and_closure_uses_current_permissions(self):
        project = self.assigned_project(worker_state=True)
        response = self.apply(project, self.preset(project, "scrum"))
        self.assertEqual(response.status_code, 200, response.content)
        states = {item["name"]: item for item in response.json()["states"]}
        task = self.task(project, state_code=states["ABIERTO"]["code"], responsible_id=self.worker.worker_id)
        path = f"{project.pk}/tasks/{task.pk}/state/"
        changed = self.request("post", path, {"state_code": states["COMPLETADO"]["code"]}, self.worker)
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(changed.json()["state_category"], "active")
        admin_task = self.request("get", f"{project.pk}/tasks/", user=self.admin).json()[0]
        self.assertTrue(admin_task["permissions"]["create_child"])
        task.refresh_from_db()
        self.assertEqual(task.progress, 0)
        self.assertEqual(self.request("post", path, {"state_code": states["CERRADO"]["code"]}, self.worker).status_code, 200)
        closed = self.request("post", path, {"state_code": states["CERRADO"]["code"]}, self.admin)
        self.assertEqual(closed.status_code, 200, closed.content)
        task.refresh_from_db()
        self.assertEqual(task.progress, 100)
        self.assertFalse(task.archived)
        self.assertEqual(self.request("post", path, {"state_code": states["EN PROGRESO"]["code"]}, self.admin).status_code, 200)

    def test_foreign_custom_codes_cannot_be_applied_or_created(self):
        first = self.project()
        other = self.project()
        states = self.apply(first, self.preset(first)).json()["states"]
        self.assertEqual(self.apply(other, states).status_code, 400)
        self.assertFalse(any(item["code"] == states[0]["code"] for item in self.configuration(other)["available_states"]))
        response = self.request("post", f"{other.pk}/tasks/", self.task_payload(state_code=states[0]["code"]), self.admin)
        self.assertEqual(response.status_code, 400)

    def test_custom_transitions_ignore_discarded_reason_and_supervision_rules(self):
        project = self.assigned_project(worker_state=True)
        states = self.apply(project, self.preset(project)).json()["states"]
        TaskTransition.objects.create(source="EN_CURSO", target="PENDIENTE", supervision_required=True, reason_required=True)
        task = self.task(project, state_code=states[1]["code"], responsible_id=self.worker.worker_id)
        path = f"{project.pk}/tasks/{task.pk}/state/"
        self.assertEqual(self.request("post", path, {"state_code": states[0]["code"]}, self.worker).status_code, 200)
        self.assertEqual(self.request("post", path, {"state_code": states[0]["code"]}, self.admin).status_code, 200)
        self.assertEqual(self.request("post", path, {"state_code": states[0]["code"], "reason": "Motivo"}, self.admin).status_code, 200)

        project.worker_state = False
        project.save(update_fields=["worker_state"])
        self.assertEqual(self.request("post", path, {"state_code": states[1]["code"]}, self.worker).status_code, 403)
        self.assertEqual(self.request("post", path, {"state_code": states[1]["code"]}, self.admin).status_code, 200)
