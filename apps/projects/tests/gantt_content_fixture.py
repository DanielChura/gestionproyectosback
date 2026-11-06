import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.projects.models import Project, Task, TaskDependency, TaskOrder
from apps.projects.services.audit import audit_actor
from .gantt_closure_fixture import closure_snapshot, require_closure_database


def prepare_content(fixture, credentials):
    require_closure_database()
    if os.environ.get("GM_GANTT_CONTENT") != "1":
        raise RuntimeError("Requires explicit content QA")
    data = credentials["context"]["controls"]
    project_id = data["project"]
    root = fixture.task(Project.objects.get(pk=project_id), name="Destino de enlace contenido QA", due_date=str(fixture.today + timedelta(days=18)))
    data["target"] = root.pk
    data["readonly_target"] = fixture.task(Project.objects.get(pk=credentials["context"]["readonly_project"]), name="Destino consulta contenido QA", responsible_id=fixture.worker.worker_id).pk
    medium = fixture.assigned_project(name="400 tareas contenido QA")
    data["medium"] = medium.pk
    with audit_actor(fixture.admin):
        Task.objects.bulk_create([
            Task(project=medium, name=f"Fila media {index:04} QA", start_date=fixture.today,
                 due_date=None if index % 7 == 0 else fixture.today + timedelta(days=10), state_code="PENDIENTE")
            for index in range(400)
        ])
        rows = list(Task.objects.filter(project=medium).order_by("pk"))
        TaskOrder.objects.bulk_create([TaskOrder(task=task, position=Decimal(index + 1) * 1024) for index, task in enumerate(rows)])
        TaskDependency.objects.create(project_id=project_id, predecessor_id=data["subject"], successor_id=data["parent"])
        TaskDependency.objects.create(project_id=project_id, predecessor_id=data["parent"], successor_id=data["tail"])
        for project in (medium.pk, data["large"]):
            large = list(Task.objects.filter(project_id=project).order_by("pk"))
            TaskDependency.objects.bulk_create([
                TaskDependency(project_id=project, predecessor_id=task.pk, successor_id=large[index + offset].pk)
                for index, task in enumerate(large) for offset in range(1, 6) if index + offset < len(large)
            ])
    output = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    with connection.cursor() as cursor:
        cursor.execute("SELECT VERSION(), @@transaction_isolation")
        mysql_version, isolation = cursor.fetchone()
    (output / "content-environment.json").write_text(json.dumps({"mysql": mysql_version, "isolation": isolation, "database": "isolated_loopback_disposable"}, indent=2), encoding="utf-8")
    measurements = []
    for project in (project_id, medium.pk, data["large"]):
        for iteration in range(3):
            start = perf_counter()
            with CaptureQueriesContext(connection) as queries:
                response = fixture.request("get", path=f"{project}/tasks/", user=fixture.admin)
            if response.status_code != 200:
                raise RuntimeError("Performance read failed")
            measurements.append({"project": project, "iteration": iteration, "rows": len(response.json()),
                                 "queries": len(queries), "bytes": len(response.content), "milliseconds": (perf_counter() - start) * 1000})
    (output / "content-api-measurements.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
    (output / "content-baseline.json").write_text(json.dumps(closure_snapshot(), default=str, indent=2), encoding="utf-8")


def verify_content():
    require_closure_database()
    if os.environ.get("GM_GANTT_CONTENT") != "1":
        raise RuntimeError("Requires explicit content QA")
    output = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    before = json.loads((output / "content-baseline.json").read_text(encoding="utf-8"))
    ledger = json.loads((output / "content-ledger.json").read_text(encoding="utf-8"))
    after = json.loads(json.dumps(closure_snapshot(), default=str))
    (output / "content-final.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    old_tasks = {row["id"]: row for row in before["task"]}
    current = {row["id"]: row for row in after["task"]}
    created = {row["id"]: row for row in ledger["created"]}
    if set(current) != set(old_tasks) | set(created):
        raise RuntimeError("Unrecorded creation or deletion")
    calendars = {int(key): row for key, row in ledger["calendars"].items()}
    for key, old in old_tasks.items():
        new = current[key]
        for field, value in old.items():
            if key in calendars and field in ("start_date", "due_date", "calendar_revision", "updated_at"):
                continue
            if new[field] != value:
                raise RuntimeError(f"Unexpected field change {key}:{field}")
    for key, saved in {**created, **calendars}.items():
        for field in ("name", "parent_id", "state_code", "start_date", "due_date", "calendar_revision"):
            if str(current[key][field]) != str(saved[field]):
                raise RuntimeError(f"Confirmed snapshot mismatch {key}:{field}")
    for saved in created.values():
        if sum(row["name"] == saved["name"] for row in current.values()) != 1:
            raise RuntimeError("Duplicate creation")
    edges = {(row["project_id"], row["predecessor_id"], row["successor_id"]) for row in before["taskdependency"]}
    for row in ledger["links"]:
        edge = (row["project"], row["source"], row["target"])
        if row["method"] == "DELETE":
            edges.remove(edge)
        else:
            edges.add(edge)
    if edges != {(row["project_id"], row["predecessor_id"], row["successor_id"]) for row in after["taskdependency"]}:
        raise RuntimeError("Unconfirmed dependency")
    for model in ("taskorder", "taskassignee", "tasklabel", "taskactivity"):
        new = {json.dumps(row, sort_keys=True) for row in after[model]}
        if any(json.dumps(row, sort_keys=True) not in new for row in before[model]):
            raise RuntimeError(f"Existing {model} changed")
    intents = after["taskcalendarintent"]
    if {row["key"] for row in intents} != set(ledger["intent_ids"]) or any(row["status"] != "succeeded" for row in intents):
        raise RuntimeError("Unexpected calendar intention")
    added = after["taskactivity"][len(before["taskactivity"]):]
    for action, method in (("dependency_added", "POST"), ("dependency_removed", "DELETE")):
        if sum(row["action"] == action for row in added) != 2 * sum(row["method"] == method for row in ledger["links"]):
            raise RuntimeError("Dependency audit count mismatch")
    (output / "content-verification.json").write_text(json.dumps({
        "preserved_tasks": len(old_tasks), "created": len(created), "logical_calendar_writes": len(intents),
        "recorded_dependency_writes": len(ledger["links"]), "unrelated_fields_history_and_order_preserved": True,
        "dependencies_match_confirmed_operations": True,
    }, indent=2), encoding="utf-8")
    print("Content snapshots, creation counts, calendars, dependencies and audit verified.", flush=True)
