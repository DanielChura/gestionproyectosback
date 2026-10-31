from datetime import timedelta
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
from apps.projects.models import Task, TaskAssignee, TaskDependency, TaskLabel, TaskOrder
from apps.projects.services.audit import audit_actor


def prepare_presentation(fixture, credentials):
    if os.environ.get("GM_GANTT_STAGE") != "5" or connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Requires isolated presentation Gantt data")
    projects = []
    for size in (24, 400, 2000):
        project = fixture.assigned_project(name=f"Gantt medición {size} QA", worker_edit=True)
        project.start_date = fixture.today - timedelta(days=30)
        project.end_date = None
        project.save(update_fields=["start_date", "end_date"])
        with audit_actor(fixture.admin):
            Task.objects.bulk_create([
                Task(project=project, name=f"Tarea {index:04} medición QA", state_code="EN_CURSO" if index % 3 else "PENDIENTE",
                     responsible_id=fixture.worker.worker_id if index % 4 == 0 else None,
                     start_date=fixture.today - timedelta(days=7) if index == 0 else fixture.today - timedelta(days=2) if index == 6 else fixture.today,
                     due_date=None if index % 7 == 3 else fixture.today - timedelta(days=7) if index == 0 else fixture.today + timedelta(days=10))
                for index in range(size)
            ])
            tasks = list(Task.objects.filter(project=project).order_by("id"))
            Task.objects.filter(pk=tasks[0].pk).update(created_at=timezone.now() - timedelta(days=10))
            for index, task in enumerate(tasks):
                if index % 20 in (11, 12):
                    Task.objects.filter(pk=task.pk).update(parent_id=tasks[index - 1].pk)
            TaskOrder.objects.bulk_create([TaskOrder(task=task, position=Decimal(index + 1) * 1024) for index, task in enumerate(tasks)])
            TaskAssignee.objects.bulk_create([TaskAssignee(task=task, worker_id=fixture.admin.worker_id) for task in tasks[::10]])
            TaskLabel.objects.bulk_create([TaskLabel(task=task, name="Revisión QA", kind="technical") for task in tasks[::8]])
            TaskDependency.objects.bulk_create([TaskDependency(project=project, predecessor=tasks[index - 20], successor=tasks[index]) for index in range(20, size, 20)])
        projects.append({"project": project.pk, "size": size, "first": tasks[0].pk, "drag": tasks[4].pk,
                         "parent": tasks[10].pk, "child": tasks[11].pk, "grandchild": tasks[12].pk,
                         "null": tasks[3].pk, "red": tasks[6].pk, "last": tasks[-1].pk})
    credentials["context"]["benchmarks"] = projects
    samples = []
    client = Client()
    for role in ("admin", "worker"):
        token = issue_access(getattr(fixture, role), timezone.now() + timedelta(minutes=20))
        for project in projects:
            for attempt in range(3):
                with CaptureQueriesContext(connection) as queries:
                    started = perf_counter()
                    response = client.get(f'/api/projects/{project["project"]}/tasks/', HTTP_AUTHORIZATION="Bearer " + token)
                    elapsed = perf_counter() - started
                if response.status_code != 200:
                    raise RuntimeError(f"Measurement failed: {response.status_code}")
                body = response.json()
                if len({task["id"] for task in body}) != len(body):
                    raise RuntimeError("Duplicate task in collection")
                if role == "admin" and len(body) != project["size"]:
                    raise RuntimeError("Truncated task collection")
                samples.append({"role": role, "size": project["size"], "rows": len(body), "attempt": attempt + 1,
                                "queries": len(queries), "elapsed_ms": round(elapsed * 1000, 3), "bytes": len(response.content)})
    Path(os.environ["GM_BROWSER_REPORT_DIR"], "queries-presentation.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
