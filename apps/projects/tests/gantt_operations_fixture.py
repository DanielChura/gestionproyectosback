from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
from threading import Event
import time
from unittest.mock import patch

from django.db import connection, connections
from django.test import Client
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.authentication.tokens import issue_access
from apps.projects.models import (
    Task, TaskActivity, TaskAssignee, TaskCalendarIntent, TaskDependency, TaskLabel, TaskOrder,
)
from apps.projects.services import tasks as service
from apps.projects.services.audit import audit_actor


def require_isolated():
    if (
        os.environ.get("GM_GANTT_STAGE") != "4"
        or connection.settings_dict["HOST"] != "127.0.0.1"
        or connection.settings_dict["NAME"] != "gm_test_task_status_templates"
    ):
        raise RuntimeError("Requires the disposable Gantt operations database")


def output(name, value):
    Path(os.environ["GM_BROWSER_REPORT_DIR"], name).write_text(
        json.dumps(value, default=str, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def domain():
    require_isolated()
    return {
        name: list(model.objects.order_by("pk").values())
        for name, model in (
            ("tasks", Task), ("history", TaskActivity), ("assignees", TaskAssignee),
            ("labels", TaskLabel), ("dependencies", TaskDependency),
            ("order", TaskOrder), ("intents", TaskCalendarIntent),
        )
    }


def dependency_race(fixture, project, first, second, duplicate=False):
    held, attempted, release = Event(), Event(), Event()
    identities, timeline = {}, []
    tokens = [issue_access(fixture.admin, timezone.now() + timedelta(minutes=5)) for _ in range(2)]
    initial_history = TaskActivity.objects.count()

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
            normalized = " ".join(sql.replace(chr(96), "").upper().split())
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
                    raise RuntimeError("First dependency operation was not released")
            return result

        successor, predecessor = (second.pk, first.pk) if index == 0 or duplicate else (first.pk, second.pk)
        try:
            with db.execute_wrapper(synchronize):
                response = Client().post(
                    f"/api/projects/{project.pk}/tasks/{successor}/dependencies/",
                    data=json.dumps({"predecessor_id": predecessor}), content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer " + tokens[index],
                )
            with db.cursor() as cursor:
                cursor.execute("SELECT @gm_usuario_id,@gm_operacion_id")
                if cursor.fetchone() != (None, None):
                    raise RuntimeError("Audit context leaked across dependency requests")
            timeline.append({"request": index, "response": time.monotonic(), "status": response.status_code})
            return response.status_code
        finally:
            db.close()

    waiting = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(run, 0)
        try:
            if not held.wait(10):
                raise RuntimeError("First dependency request did not acquire the project lock")
            two = pool.submit(run, 1)
            if not attempted.wait(10):
                raise RuntimeError("Second request did not attempt the project lock")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID "
                        "WHERE r.PROCESSLIST_ID=%s AND b.PROCESSLIST_ID=%s",
                        [identities[1], identities[0]],
                    )
                    waiting = cursor.fetchone() is not None
                if waiting:
                    break
                time.sleep(0.02)
            if not waiting:
                raise RuntimeError("No actual overlapping dependency lock wait observed")
        finally:
            release.set()
        statuses = [one.result(timeout=15), two.result(timeout=15)]
    if statuses != [201, 400] or len(set(identities.values())) != 2:
        raise RuntimeError("Concurrent dependencies were not serialized correctly")
    if TaskDependency.objects.filter(project=project).count() != 1:
        raise RuntimeError("Duplicate or cyclic dependency persisted")
    if TaskActivity.objects.count() != initial_history + 2:
        raise RuntimeError("Rejected concurrent dependency fabricated history")
    return {
        "scenario": "duplicate" if duplicate else "opposite_edges",
        "connections": identities, "lock_wait_observed": waiting, "statuses": statuses, "timeline": timeline,
        "one_edge": True, "audit_events": 2, "audit_context_released": True,
    }


def prepare_operations(fixture, credentials):
    require_isolated()
    races = []
    for duplicate in (False, True):
        project = fixture.assigned_project(name="Dependencias concurrentes QA")
        first, second = fixture.task(project), fixture.task(project)
        races.append(dependency_race(fixture, project, first, second, duplicate))
    project = fixture.assigned_project(name="Rollback de dependencias QA")
    first, second = fixture.task(project), fixture.task(project)
    before = domain()
    record = service.record_activity
    calls = 0

    def broken_audit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Injected isolated audit failure")
        return record(*args, **kwargs)

    with patch.object(service, "record_activity", side_effect=broken_audit):
        try:
            service.manage_dependency(fixture.admin, project.pk, second.pk, first.pk)
        except RuntimeError as error:
            if str(error) != "Injected isolated audit failure":
                raise
        else:
            raise RuntimeError("Rollback injection was not reached")
    if domain() != before:
        raise RuntimeError("Dependency or audit escaped rollback")
    child = fixture.task(project, parent_id=first.pk)
    foreign = fixture.task(fixture.assigned_project())
    invalid = []
    before = domain()
    for user, target, predecessor in (
        (fixture.admin, first.pk, first.pk), (fixture.admin, second.pk, child.pk),
        (fixture.admin, second.pk, foreign.pk), (fixture.worker, second.pk, first.pk),
    ):
        try:
            service.manage_dependency(user, project.pk, target, predecessor)
        except APIException as error:
            invalid.append(error.status_code)
        else:
            raise RuntimeError("Invalid dependency was accepted")
    if invalid != [400, 400, 404, 403] or domain() != before:
        raise RuntimeError("Invalid dependencies changed the domain")
    output("backend-operations-tests.json", {
        "races": races, "rollback_dependency_and_both_audits": True,
        "invalid_self_child_foreign_worker": invalid, "invalid_operations_unchanged": True,
    })
    project = fixture.assigned_project(name="Gantt gestión QA", worker_edit=True, worker_contribute=True)
    project.start_date = fixture.today - timedelta(days=20)
    project.end_date = fixture.today + timedelta(days=60)
    project.save(update_fields=["start_date", "end_date"])
    root = fixture.task(
        project, name="Principal de gestión QA", due_date=str(fixture.today + timedelta(days=12)),
        responsible_id=fixture.worker.worker_id,
    )
    child = fixture.task(
        project, name="Subtarea con estado propio QA", parent_id=root.pk, state_code="EN_CURSO",
        due_date=str(fixture.today + timedelta(days=4)), responsible_id=fixture.worker.worker_id,
    )
    null = fixture.task(project, name="Principal sin límite de gestión QA", due_date=None)
    outside = fixture.task(
        project, name="Principal fuera del rango QA", due_date=str(fixture.today + timedelta(days=50)),
    )
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=outside.pk).update(start_date=fixture.today + timedelta(days=45))
    for index in range(34):
        fixture.task(project, name=f"Candidata de gestión QA {index:02d}", due_date=None)
    credentials["context"].update(
        operations_project=project.pk, operations_root=root.pk, operations_child=child.pk,
        operations_null=null.pk, operations_outside=outside.pk, operations_count=38,
        operations_worker=fixture.worker.worker_id,
    )
    output("operations-baseline.json", domain())


def verify_operations():
    require_isolated()
    root = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    before = json.loads((root / "operations-baseline.json").read_text(encoding="utf-8"))
    after = json.loads(json.dumps(domain(), default=str))
    output("operations-final.json", after)
    ledger = json.loads((root / "operations-ledger.json").read_text(encoding="utf-8"))
    expected = {int(key): value for key, value in ledger["tasks"].items()}
    original = {item["id"]: item for item in before["tasks"]}
    current = {item["id"]: item for item in after["tasks"]}
    created = set(ledger["created_ids"])
    if set(current) != set(original) | created:
        raise RuntimeError("Unexpected task creation or deletion")
    for task_id, old in original.items():
        saved = current[task_id]
        for field in ("parent_id", "start_date", "due_date", "calendar_revision", "estimated_minutes"):
            if saved[field] != old[field]:
                raise RuntimeError("Management changed hierarchy or calendar")
        if task_id not in expected and saved != old:
            raise RuntimeError("Unrecorded task modification")
    for task_id, task in expected.items():
        saved = current[task_id]
        for field in ("name", "parent_id", "state_code", "priority", "responsible_id", "start_date", "due_date"):
            if str(saved[field]) != str(task[field]):
                raise RuntimeError(f"Persisted task differs from browser: {task_id} {field}")
        member_ids = set(TaskAssignee.objects.filter(task_id=task_id).values_list("worker_id", flat=True))
        if saved["responsible_id"] is not None:
            member_ids.add(saved["responsible_id"])
        members = sorted(member_ids)
        if members != sorted(item["id"] for item in task["assignees"]):
            raise RuntimeError("Assignees differ from confirmed task")
        labels = list(TaskLabel.objects.filter(task_id=task_id).values("id", "name", "specialty__name"))
        if sorted(item["id"] for item in labels) != sorted(item["id"] for item in task["labels"]):
            raise RuntimeError("Label identities differ from confirmed task")
        names = sorted(item["specialty__name"] or item["name"] for item in labels)
        if names != sorted(item["name"] for item in task["labels"]):
            raise RuntimeError("Labels differ from confirmed task")
    for name in ledger["creation_names"]:
        if sum(item["name"] == name for item in after["tasks"]) != 1:
            raise RuntimeError("A creation was duplicated")
    expected_edges = {
        (item["project_id"], item["predecessor_id"], item["successor_id"])
        for item in ledger["dependencies"]
    }
    actual_edges = {
        (item["project_id"], item["predecessor_id"], item["successor_id"])
        for item in after["dependencies"]
    }
    if expected_edges != actual_edges:
        raise RuntimeError("Persisted dependencies differ from the browser ledger")
    positions = {item["task_id"]: item for item in after["order"]}
    if any(positions.get(item["task_id"]) != item for item in before["order"]):
        raise RuntimeError("Task management reordered existing tasks")
    if after["intents"] != before["intents"]:
        raise RuntimeError("Management unexpectedly edited calendar")
    previous = {item["id"]: item for item in before["history"]}
    history = {item["id"]: item for item in after["history"]}
    if any(history.get(key) != item for key, item in previous.items()):
        raise RuntimeError("Previous history changed")
    new_events = [item for key, item in history.items() if key not in previous]
    for action, method in (("dependency_added", "POST"), ("dependency_removed", "DELETE")):
        expected_count = 2 * sum(item["method"] == method for item in ledger["dependency_operations"])
        if sum(item["action"] == action for item in new_events) != expected_count:
            raise RuntimeError("A dependency request missed or duplicated audit events")
    for task_id in created:
        if sum(item["task_id"] == task_id and item["action"] == "created" for item in new_events) != 1:
            raise RuntimeError("A task creation missed or duplicated its audit event")
    for name in ("assignees", "labels"):
        old = [item for item in before[name] if item["task_id"] not in expected]
        current_rows = [item for item in after[name] if item["task_id"] not in expected]
        if current_rows != old:
            raise RuntimeError("Unrelated assignments or labels changed")
    output("operations-verification.json", {
        "created_tasks": len(created), "created_once": True,
        "calendar_and_hierarchy_preserved": True, "existing_order_preserved": True,
        "dependencies_match_confirmed_ledger": True, "previous_history_preserved": True,
        "new_history_events": len(history) - len(previous), "unrelated_tasks_preserved": True,
        "dependency_audit_events": sum(item["action"].startswith("dependency_") for item in new_events),
        "one_creation_audit_per_task": True, "unrelated_assignments_and_labels_preserved": True,
    })
    print("Task operations persisted once; calendars, hierarchy, order and existing audit preserved.", flush=True)
