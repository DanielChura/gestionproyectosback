from datetime import timedelta
import unittest
from uuid import uuid4
from unittest.mock import patch

from django.db import connection
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.projects.models import Task, TaskActivity, TaskCalendarIntent, TaskDependency, Tema
from apps.projects.services.scheduling import planned_window, schedule_value
from . import test_projects as fixtures


class SchedulingContractTests(unittest.TestCase):
    account = fixtures.ProjectFlowTests.account
    request = fixtures.ProjectFlowTests.request
    project_payload = fixtures.ProjectFlowTests.project_payload
    project = fixtures.ProjectFlowTests.project
    assigned_project = fixtures.ProjectFlowTests.assigned_project
    def task_payload(self, **changes):
        data = fixtures.ProjectFlowTests.task_payload(self, **changes)
        if "schedule" in changes:
            data.pop("due_date", None)
        return data
    task = fixtures.ProjectFlowTests.task
    setUp = fixtures.ProjectFlowTests.setUp
    tearDown = fixtures.ProjectFlowTests.tearDown

    @classmethod
    def setUpClass(cls):
        fixtures.ProjectFlowTests.setUpClass.__func__(cls)
        with connection.schema_editor() as editor:
            editor.create_model(Tema)
        cls.tables.append(Tema)

    tearDownClass = classmethod(fixtures.ProjectFlowTests.tearDownClass.__func__)

    def hour(self, start=9, end=12, day=0):
        date = str(self.today + timedelta(days=day))
        return {"precision": "hour", "start": f"{date}T{start:02d}:00:00-05:00",
                "end": f"{date}T{end:02d}:00:00-05:00" if end is not None else None, "timezone": "America/Lima"}

    def patch_task(self, task, **changes):
        task.refresh_from_db()
        payload = {"expected_calendar_revision": str(task.calendar_revision), "calendar_intent_id": str(uuid4()), **changes}
        return self.request("patch", f"{task.project_id}/tasks/{task.pk}/", payload, self.admin)

    def edge(self, predecessor, successor, kind, method="post", **extra):
        return self.request(method, f"{successor.project_id}/tasks/{successor.pk}/dependencies/",
                            {"predecessor_id": predecessor.pk, "dependency_type": kind, **extra}, self.admin)

    def state(self, task, code, user=None):
        return self.request("post", f"{task.project_id}/tasks/{task.pk}/state/", {"state_code": code}, user or self.admin)

    def test_hour_canonical_roundtrip_legacy_date_edits_preserve_time_and_revision(self):
        project = self.project()
        task = self.task(project, schedule=self.hour(9, 10))
        self.assertEqual(task.calendar_revision, 1)
        self.assertEqual(task.start_time.hour, 9)
        self.assertEqual(schedule_value(task)["end"], self.hour(9, 10)["end"])
        response = self.patch_task(task, start_date=str(self.today + timedelta(days=1)), due_date=str(self.today + timedelta(days=1)))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["schedule"]["start"], self.hour(9, 10, 1)["start"])
        self.assertEqual(response.json()["calendar_revision"], "2")
        self.assertEqual(task.parent_id, None)

    def test_midnight_end_is_exclusive_without_legacy_projection_day_shift(self):
        schedule = self.hour(23, 0)
        schedule["end"] = str(self.today + timedelta(days=1)) + "T00:00:00-05:00"
        task = self.task(self.project(), schedule=schedule)
        self.assertEqual(task.due_date, self.today)
        self.assertEqual(schedule_value(task), schedule)
        self.assertEqual(planned_window(task)[1] - planned_window(task)[0], timedelta(hours=1))

    def test_hour_minimum_timezone_and_mixed_representation_rejected(self):
        project = self.project()
        for schedule in [self.hour(9, 9), {**self.hour(), "timezone": "UTC"}, {**self.hour(), "start": str(self.today) + "T09:00:00"}]:
            result = self.request("post", f"{project.pk}/tasks/", self.task_payload(schedule=schedule), self.admin)
            self.assertEqual(result.status_code, 400, result.content)
        result = self.request("post", f"{project.pk}/tasks/", self.task_payload(schedule=self.hour(), start_date=str(self.today)), self.admin)
        self.assertEqual(result.status_code, 400)
        self.assertFalse(Task.objects.exists())

    def test_day_nullable_and_historical_calendar_remain_days_without_real_execution(self):
        task = self.task(self.project(), due_date=None)
        self.assertEqual(schedule_value(task), {"precision": "day", "start": str(self.today), "end": None, "timezone": "America/Lima"})
        self.assertIsNone(task.start_time)
        self.assertIsNone(task.actual_started_at)
        self.assertIsNone(task.actual_finished_at)
        self.assertEqual(self.patch_task(task, name="Solo nombre").status_code, 400)
        result = self.request("patch", f"{task.project_id}/tasks/{task.pk}/", {"name": "Solo nombre"}, self.admin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["calendar_revision"], "1")
        from apps.projects.services.presentation import task_snapshot
        task.state_code = "QA_PENDING"
        with patch("apps.projects.services.presentation.configured_states", return_value=[{"code": "QA_PENDING", "name": "Por hacer", "category": "not_started"}]):
            self.assertFalse(task_snapshot(task, task.project, self.admin)["execution"]["historical_unknown"])


    def test_four_planning_types_and_rejection_rolls_back_intent_and_history(self):
        for kind, start, end, invalid in [("FS", 12, 14, self.hour(11, 14)), ("SS", 10, 13, self.hour(8, 13)),
                                           ("FF", 8, 12, self.hour(8, 11)), ("SF", 6, 10, self.hour(6, 8))]:
            with self.subTest(kind=kind):
                project = self.project()
                predecessor = self.task(project, schedule=self.hour(9, 12))
                successor = self.task(project, schedule=self.hour(start, end))
                result = self.edge(predecessor, successor, kind)
                self.assertEqual(result.status_code, 201, result.content)
                count, intents = TaskActivity.objects.count(), TaskCalendarIntent.objects.count()
                result = self.patch_task(successor, schedule=invalid)
                self.assertEqual(result.status_code, 400, result.content)
                self.assertEqual(result.json()["code"], "dependency_constraint")
                self.assertEqual(TaskActivity.objects.count(), count)
                self.assertEqual(TaskCalendarIntent.objects.count(), intents)
                successor.refresh_from_db()
                self.assertEqual(successor.calendar_revision, 1)

    def test_registration_preserves_explicit_fs_and_ss_with_same_day_plans(self):
        project = self.project()
        a = self.task(project, due_date=str(self.today))
        b = self.task(project, start_date=str(self.today), due_date=str(self.today))
        self.assertEqual(self.edge(a, b, "FS").status_code, 201)
        self.assertEqual(self.edge(a, b, "FS", "delete").status_code, 204)
        self.assertEqual(self.edge(a, b, "SS").status_code, 201)
        self.assertEqual(self.edge(a, b, "FS", "patch", expected_type="SS").status_code, 200)
        self.assertEqual(self.patch_task(b, start_date=str(self.today + timedelta(days=1)), due_date=str(self.today + timedelta(days=1))).status_code, 200)
        self.assertEqual(self.edge(a, b, "FS", "patch", expected_type="FS").status_code, 200)

    def test_four_types_gate_correct_real_event_and_direct_completion_checks_both(self):
        for kind in ["FS", "SS", "FF", "SF"]:
            with self.subTest(kind=kind):
                project = self.project()
                a = self.task(project, schedule=self.hour(9, 10))
                b = self.task(project, schedule=self.hour(11, 12))
                self.assertEqual(self.edge(a, b, kind).status_code, 201)
                if kind in ("FF", "SF"):
                    self.assertEqual(self.state(b, "EN_CURSO").status_code, 200)
                else:
                    self.assertEqual(self.state(b, "EN_CURSO").status_code, 400)
                self.assertEqual(self.state(b, "COMPLETADA").status_code, 400)
                if kind[0] == "F":
                    self.assertEqual(self.state(a, "CANCELADA").status_code, 200)
                    self.assertEqual(self.state(b, "COMPLETADA").status_code, 400)
                self.assertEqual(self.state(a, "EN_CURSO").status_code, 200)
                if kind[0] == "F":
                    self.assertEqual(self.state(a, "COMPLETADA").status_code, 200)
                self.assertEqual(self.state(b, "COMPLETADA").status_code, 200)
                b.refresh_from_db()
                self.assertIsNotNone(b.actual_started_at)
                self.assertIsNotNone(b.actual_finished_at)
                self.assertEqual(self.state(a, "PENDIENTE").status_code, 400)

    def test_all_incoming_constraints_and_one_to_many_are_independent(self):
        project = self.project()
        a, c = [self.task(project, schedule=self.hour(8, 9)) for _ in range(2)]
        b, d = [self.task(project, schedule=self.hour(10, 12)) for _ in range(2)]
        for predecessor, successor in [(a, b), (c, b), (a, d)]:
            self.assertEqual(self.edge(predecessor, successor, "SS").status_code, 201)
        self.assertEqual(self.state(a, "EN_CURSO").status_code, 200)
        self.assertEqual(self.state(b, "EN_CURSO").status_code, 400)
        self.assertEqual(self.state(d, "EN_CURSO").status_code, 200)
        self.assertEqual(self.state(c, "EN_CURSO").status_code, 200)
        self.assertEqual(self.state(b, "EN_CURSO").status_code, 200)

    def test_origin_edits_reopen_and_cancellation_cannot_invalidate_executed_successors(self):
        project = self.project()
        a = self.task(project, schedule=self.hour(8, 9))
        b = self.task(project, schedule=self.hour(10, 12))
        self.assertEqual(self.edge(a, b, "FS").status_code, 201)
        self.assertEqual(self.state(a, "COMPLETADA").status_code, 200)
        self.assertEqual(self.state(b, "EN_CURSO").status_code, 200)
        for target in ["PENDIENTE", "EN_CURSO", "CANCELADA"]:
            self.assertEqual(self.state(a, target).status_code, 400)
        self.assertEqual(self.patch_task(a, schedule=self.hour(8, 11)).status_code, 400)
        a.refresh_from_db()
        self.assertEqual(a.state_code, "COMPLETADA")

    def test_graph_permissions_cycles_duplicates_scope_and_explicit_type(self):
        project = self.project()
        a,b,c = [self.task(project, schedule=self.hour()) for _ in range(3)]
        for source,target in [(a,b),(b,c)]:
            self.assertEqual(self.edge(source,target,"SS").status_code,201)
        for source,target in [(c,a),(a,b),(a,a)]:
            self.assertEqual(self.edge(source,target,"SS").status_code,400)
        foreign = self.task(self.project(), schedule=self.hour())
        self.assertEqual(self.edge(foreign,a,"SS").status_code,404)
        child = self.task(project,parent_id=a.pk,schedule=self.hour())
        self.assertEqual(self.edge(child,c,"SS").status_code,201)
        for user in [self.worker,self.outsider]:
            result=self.request("post",f"{project.pk}/tasks/{b.pk}/dependencies/",{"predecessor_id":a.pk,"dependency_type":"SS"},user)
            self.assertEqual(result.status_code,403)
        result=self.request("post",f"{project.pk}/tasks/{a.pk}/dependencies/",{"predecessor_id":c.pk},self.admin)
        self.assertEqual(result.status_code,400)

    def test_legacy_stays_informative_and_conversion_preserves_type_without_retroactive_proof(self):
        project = self.project()
        a,b = [self.task(project, schedule=self.hour()) for _ in range(2)]
        if connection.vendor == "mysql":
            return
        TaskDependency.objects.create(project=project,predecessor=a,successor=b)
        self.assertEqual(self.state(b,"EN_CURSO").status_code,200)
        response=self.edge(a,b,"SS","patch",expected_type=None)
        self.assertEqual(response.status_code,200)
        self.assertEqual(TaskDependency.objects.get(project=project,predecessor=a,successor=b).dependency_type, "SS")
        self.assertEqual(self.state(b,"PENDIENTE").status_code,200)
        self.assertEqual(self.edge(a,b,"SS","patch",expected_type="SS").status_code,200)
        self.assertEqual(self.edge(a,b,"FF","patch",expected_type=None).status_code,409)

    def test_movement_endpoint_uses_identical_execution_validation(self):
        project=self.project()
        a=self.task(project,schedule=self.hour(8,9));b=self.task(project,schedule=self.hour(10,12))
        self.assertEqual(self.edge(a,b,"FS").status_code,201)
        snapshot=self.request("get",f"{project.pk}/tasks/{b.pk}/",user=self.admin).json()["task"]
        response=self.request("post",f"{project.pk}/tasks/{b.pk}/move/",{"state_code":"COMPLETADA","before_id":None,"expected_revision":snapshot["move_revision"]},self.admin)
        self.assertEqual(response.status_code,400)
        b.refresh_from_db();self.assertEqual(b.state_code,"PENDIENTE")

    def test_hour_same_intent_replays_current_calendar_without_double_audit(self):
        task=self.task(self.project(),schedule=self.hour(8,9))
        payload={"schedule":self.hour(9,10),"expected_calendar_revision":"1","calendar_intent_id":str(uuid4())}
        path=f"{task.project_id}/tasks/{task.pk}/"
        first=self.request("patch",path,payload,self.admin)
        self.assertEqual(first.status_code,200,first.content)
        count=TaskActivity.objects.count()
        self.assertEqual(self.request("patch",path,payload,self.admin).status_code,200)
        self.assertEqual(TaskActivity.objects.count(),count)
        self.assertEqual(self.patch_task(task,schedule=self.hour(10,11)).status_code,200)
        repeated=self.request("patch",path,payload,self.admin)
        self.assertEqual(repeated.status_code,200)
        self.assertEqual(repeated.json()["schedule"],self.hour(10,11))
        self.assertTrue(repeated.json()["calendar_receipt"]["replayed"])

    def test_precise_family_bounds_without_cascade(self):
        project=self.project()
        parent=self.task(project,schedule=self.hour(8,12))
        child=self.task(project,parent_id=parent.pk,schedule=self.hour(9,10))
        response=self.patch_task(child,schedule=self.hour(11,13))
        self.assertEqual(response.status_code,400)
        response=self.patch_task(parent,schedule=self.hour(10,12))
        self.assertEqual(response.status_code,400)
        parent.refresh_from_db();child.refresh_from_db()
        self.assertEqual(parent.start_time.hour,8);self.assertEqual(child.start_time.hour,9)

    def test_both_calendar_extremes_change_once_and_hour_null_recovers_explicitly(self):
        task=self.task(self.project(),schedule=self.hour(8,9))
        response=self.patch_task(task,schedule=self.hour(10,12))
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()["calendar_revision"],"2")
        self.assertEqual(response.json()["schedule"],self.hour(10,12))
        response=self.patch_task(task,due_date=None)
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()["schedule"]["end"])
        response=self.patch_task(task,due_date=str(self.today))
        self.assertEqual(response.status_code,400)
        response=self.patch_task(task,schedule=self.hour(10,11))
        self.assertEqual(response.status_code,200)

    def test_state_templates_do_not_change_execution_or_weaken_existing_edges(self):
        from apps.projects.services.statuses import reassign_tasks
        from django.db import transaction
        project=self.project();a=self.task(project,schedule=self.hour(8,9));b=self.task(project,schedule=self.hour(10,11))
        self.assertEqual(self.edge(a,b,"FS").status_code,201)
        states=[{"code":"PENDIENTE","name":"Pendiente","category":"not_started","tema_id":6},
                {"code":"EN_CURSO","name":"En curso","category":"active","tema_id":2}]
        before=TaskActivity.objects.count()
        with self.assertRaises(ValidationError),transaction.atomic():
            reassign_tasks(project,states,states,{"PENDIENTE":"EN_CURSO"})
        b.refresh_from_db();self.assertEqual(b.state_code,"PENDIENTE")
        self.assertIsNone(b.actual_started_at)
        self.assertEqual(TaskActivity.objects.count(),before)

    def test_completed_historical_links_and_calendar_keep_state_parent_and_real_history(self):
        from apps.projects.services.audit import audit_actor
        for kind in ("FS", "SS", "FF", "SF"):
            with self.subTest(kind=kind):
                project = self.project()
                predecessor = self.task(project, start_date=str(self.today + timedelta(days=2)))
                parent = self.task(project)
                child = self.task(project, parent_id=parent.pk, due_date=None)
                self.assertEqual(self.state(child, "COMPLETADA").status_code, 200)
                self.assertEqual(self.state(parent, "COMPLETADA").status_code, 200)
                with audit_actor(self.admin):
                    Task.objects.filter(pk__in=[parent.pk, child.pk]).update(actual_started_at=None, actual_finished_at=None)
                child.refresh_from_db()
                before = (child.state_code, child.parent_id, child.actual_started_at, child.actual_finished_at)
                candidates = self.request("get", f"{project.pk}/tasks/{child.pk}/related-candidates/?kind=blocked_by", user=self.admin)
                self.assertEqual(candidates.status_code, 200, candidates.content)
                self.assertIn(predecessor.pk, [item["id"] for item in candidates.json()["items"]])
                candidates = self.request("get", f"{project.pk}/tasks/{predecessor.pk}/related-candidates/?kind=blocks", user=self.admin)
                self.assertIn(child.pk, [item["id"] for item in candidates.json()["items"]])
                result = self.edge(predecessor, child, kind)
                self.assertEqual(result.status_code, 201, result.content)
                result = self.patch_task(child, due_date=str(self.today + timedelta(days=1)))
                self.assertEqual(result.status_code, 200, result.content)
                child.refresh_from_db()
                self.assertEqual((child.state_code, child.parent_id, child.actual_started_at, child.actual_finished_at), before)
                self.assertEqual(TaskDependency.objects.get(project=project, predecessor=predecessor, successor=child).dependency_type, kind)
                self.assertEqual(self.edge(predecessor, child, kind).status_code, 400)
                self.assertEqual(self.edge(child, predecessor, kind).status_code, 400)
                snapshot = self.request("get", f"{project.pk}/tasks/{child.pk}/", user=self.admin).json()["task"]
                self.assertTrue(snapshot["permissions"]["dependencies"])

    def test_completed_nested_parent_can_complete_dates_without_changing_child_or_history(self):
        from apps.projects.services.audit import audit_actor

        project = self.project()
        parent = self.task(project)
        hello = self.task(project, parent_id=parent.pk, due_date=None)
        child = self.task(project, parent_id=hello.pk, due_date=None)
        self.assertEqual(self.state(child, "BLOQUEADA").status_code, 200)
        self.assertEqual(self.state(hello, "COMPLETADA").status_code, 200)
        with audit_actor(self.admin):
            Task.objects.filter(pk=hello.pk).update(actual_started_at=None, actual_finished_at=None)
        hello.refresh_from_db()
        before = (hello.state_code, hello.parent_id, hello.actual_started_at, hello.actual_finished_at)
        child_before = Task.objects.filter(pk=child.pk).values().get()
        result = self.patch_task(hello, due_date=str(self.today + timedelta(days=2)))
        self.assertEqual(result.status_code, 200, result.content)
        hello.refresh_from_db()
        self.assertEqual(hello.due_date, self.today + timedelta(days=2))
        self.assertEqual((hello.state_code, hello.parent_id, hello.actual_started_at, hello.actual_finished_at), before)
        self.assertEqual(Task.objects.filter(pk=child.pk).values().get(), child_before)
        result = self.patch_task(hello, start_date=str(self.today + timedelta(days=1)))
        self.assertEqual(result.status_code, 400, result.content)
        hello.refresh_from_db()
        self.assertEqual(hello.start_date, self.today)
        self.assertEqual((hello.state_code, hello.parent_id, hello.actual_started_at, hello.actual_finished_at), before)

    def test_recording_incompatible_plans_does_not_allow_a_future_conditioned_event(self):
        project = self.project()
        a = self.task(project, schedule=self.hour(13, 14))
        b = self.task(project, schedule=self.hour(9, 10))
        self.assertEqual(self.edge(a, b, "FS").status_code, 201)
        self.assertEqual(self.state(b, "EN_CURSO").status_code, 400)
        self.assertEqual(self.state(a, "COMPLETADA").status_code, 200)
        self.assertEqual(self.state(b, "EN_CURSO").status_code, 200)

    def test_delete_receipt_restores_original_type_and_validates_actor_direction_and_expiry(self):
        project = self.project()
        a, b, c = self.task(project), self.task(project), self.task(project)
        path = f"{project.pk}/tasks/{b.pk}/dependencies/"
        for kind in ("FS", "SS", "FF", "SF", None):
            with self.subTest(kind=kind):
                if kind is None:
                    from apps.projects.services.dependency_undo import restoring_legacy
                    with restoring_legacy(True):
                        TaskDependency.objects.create(project=project, predecessor=a, successor=b)
                else:
                    self.assertEqual(self.edge(a, b, kind).status_code, 201)
                removed = self.request("delete", path, {"predecessor_id": a.pk}, self.admin)
                self.assertEqual(removed.status_code, 204, removed.content)
                self.assertFalse(TaskDependency.objects.filter(predecessor=a, successor=b).exists())
                token = removed["X-GM-Dependency-Undo"]
                self.assertIn("X-GM-Dependency-Undo", removed["Access-Control-Expose-Headers"])
                self.assertIn("X-GM-Dependency-Undo-Expires", removed["Access-Control-Expose-Headers"])
                self.assertLessEqual(int(removed["X-GM-Dependency-Undo-Expires"]), (timezone.now().timestamp() + 10) * 1000)
                payload = {"predecessor_id": a.pk, "undo_token": token}
                self.assertEqual(self.request("post", path, {**payload, "undo_token": token + "invalid"}, self.admin).status_code, 400)
                self.assertEqual(self.request("post", path, {**payload, "predecessor_id": c.pk}, self.admin).status_code, 400)
                with patch("apps.projects.services.dependency_undo.timezone.now", return_value=timezone.now() + timedelta(seconds=11)):
                    self.assertEqual(self.request("post", path, payload, self.admin).status_code, 400)
                self.assertEqual(self.request("post", path, payload, self.worker).status_code, 403)
                restored = self.request("post", path, payload, self.admin)
                self.assertEqual(restored.status_code, 201, restored.content)
                self.assertEqual(TaskDependency.objects.get(predecessor=a, successor=b).dependency_type, kind)
                self.assertEqual(self.request("post", path, payload, self.admin).status_code, 400)
                self.assertEqual(self.edge(b, a, "FS").status_code, 400)
                self.assertEqual(self.request("delete", path, {"predecessor_id": a.pk}, self.admin).status_code, 204)
                if connection.vendor == "mysql":
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT @gm_dependency_restore")
                        self.assertNotEqual(cursor.fetchone()[0], 1)
