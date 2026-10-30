from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from time import perf_counter

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.authentication.tokens import issue_access
from apps.projects.models import Project, Task, TaskAssignee, TaskDependency, TaskLabel, TaskOrder
from apps.projects.services.audit import audit_actor


def prepare_operational(fixture, credentials):
    if os.environ.get("GM_GANTT_STAGE") != "2" or connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Requires the isolated operational Gantt fixture")
    project = fixture.assigned_project(name="Gantt operativo QA")
    Project.objects.filter(pk=project.pk).update(start_date=date(1900, 1, 1), end_date=None)
    project.refresh_from_db()
    root = fixture.task(project, name="Raíz operativa QA", responsible_id=fixture.worker.worker_id, due_date=str(fixture.today + timedelta(days=20)))
    child = fixture.task(project, name="Hija con estado propio QA", parent_id=root.pk, state_code="EN_CURSO", due_date=str(fixture.today + timedelta(days=15)))
    grandchild = fixture.task(project, name="Nieto sin límite QA", parent_id=child.pk, due_date=None, state_code="BLOQUEADA")
    leaf = fixture.task(project, name="Cuarto nivel búsqueda QA", parent_id=grandchild.pk, due_date=None, labels=[{"name": "Revisión profunda QA", "kind": "technical"}])
    start = fixture.today
    records = []
    with audit_actor(fixture.admin):
        Task.objects.bulk_create([
            Task(project=project, name=f"Rama QA {index:03}", state_code="PENDIENTE",
                 responsible_id=fixture.worker.worker_id if index % 3 == 0 else None,
                 start_date=start, due_date=None if index % 3 == 0 else start + timedelta(days=10))
            for index in range(72)
        ])
        roots = list(Task.objects.filter(project=project, name__startswith="Rama QA").order_by("id"))
        for index, parent in enumerate(roots):
            for level in range(4):
                records.append(Task(
                    project=project, parent=parent, name=f"Subtarea QA {index:03}-{level}",
                    state_code="EN_CURSO" if level % 2 else "PENDIENTE",
                    responsible_id=fixture.worker.worker_id if index % 3 == 0 else None,
                    start_date=start, due_date=None if level % 2 == 0 else start + timedelta(days=5),
                ))
        Task.objects.bulk_create(records)
        extreme = Task.objects.create(project=project, name="Extremo futuro QA", state_code="PENDIENTE",
                                      start_date=date(2099, 1, 1), due_date=date(2099, 1, 1))
        historical = Task.objects.create(project=project, name="Extremo histórico QA", state_code="PENDIENTE",
                                         start_date=date(1901, 1, 1), due_date=date(1901, 1, 1))
        second = Task.objects.create(project=project, name="Sucesora operativa QA", state_code="EN_CURSO",
                                     responsible_id=fixture.worker.worker_id, start_date=start, due_date=start + timedelta(days=8))
        hidden = Task.objects.create(project=project, name="Predecesora no asignada QA", state_code="PENDIENTE",
                                     start_date=start, due_date=start + timedelta(days=2))
        TaskAssignee.objects.create(task=child, worker_id=fixture.admin.worker_id)
        TaskLabel.objects.create(task=second, name="Entrega operativa QA", kind="technical")
        TaskDependency.objects.bulk_create([
            TaskDependency(project=project, predecessor=root, successor=second),
            TaskDependency(project=project, predecessor=hidden, successor=second),
            TaskDependency(project=project, predecessor=second, successor=extreme),
        ])
        ordered = list(Task.objects.filter(project=project).order_by("-id").values_list("pk", flat=True))
        TaskOrder.objects.filter(task__project=project).delete()
        TaskOrder.objects.bulk_create([TaskOrder(task_id=pk, position=Decimal(index + 1) * 1024) for index, pk in enumerate(ordered)])
    credentials["context"].update(
        operational_project=project.pk, deep_root=root.pk, deep_child=child.pk,
        deep_grandchild=grandchild.pk, deep_leaf=leaf.pk, extreme=extreme.pk,
        far_history=historical.pk, successor=second.pk, hidden_predecessor=hidden.pk,
        operational_count=len(ordered), branch=roots[-1].pk,
    )
    measure_collections(fixture, credentials)


def measure_collections(fixture, credentials):
    samples = []
    client = Client()
    for role in ("admin", "worker"):
        token = issue_access(getattr(fixture, role), timezone.now() + timedelta(minutes=15))
        for project_id in (credentials["context"]["project"], credentials["context"]["operational_project"]):
            counts = []
            for attempt in range(3):
                with CaptureQueriesContext(connection) as queries:
                    started = perf_counter()
                    response = client.get(f"/api/projects/{project_id}/tasks/", HTTP_AUTHORIZATION="Bearer " + token)
                    elapsed = perf_counter() - started
                if response.status_code != 200:
                    raise RuntimeError(f"Collection measurement failed: {response.status_code}")
                body = response.json()
                ids = {entry["id"] for entry in body}
                if len(ids) != len(body):
                    raise RuntimeError("The shared collection returned duplicate tasks")
                counts.append(len(queries))
                samples.append({"role": role, "project": project_id, "attempt": attempt + 1,
                                "rows": len(body), "queries": len(queries),
                                "elapsed_seconds": round(elapsed, 6), "response_bytes": len(response.content)})
            if len(set(counts)) != 1:
                raise RuntimeError("Query counts changed between equivalent reads")
    admin = [sample for sample in samples if sample["role"] == "admin"]
    if max(sample["queries"] for sample in admin) - min(sample["queries"] for sample in admin) > 2:
        raise RuntimeError("Query growth requires review before Gantt acceptance")
    if max(sample["rows"] for sample in admin) != credentials["context"]["operational_count"]:
        raise RuntimeError("The shared task collection was truncated")
    Path(os.environ["GM_BROWSER_REPORT_DIR"], "queries.json").write_text(
        json.dumps(samples, indent=2), encoding="utf-8",
    )
