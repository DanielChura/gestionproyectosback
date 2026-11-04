from datetime import date, timedelta
from decimal import Decimal
import os

from django.db import connection

from apps.projects.models import Project, Task, TaskAssignee, TaskOrder
from apps.projects.services.audit import audit_actor


def prepare_controls(fixture, credentials):
    if (
        os.environ.get("GM_GANTT_CONTROLS") != "1"
        or os.environ.get("GM_GANTT_STAGE") != "6"
        or connection.settings_dict["HOST"] != "127.0.0.1"
        or connection.settings_dict["NAME"] != "gm_test_task_status_templates"
    ):
        raise RuntimeError("Requires the isolated Gantt controls database")
    project = fixture.assigned_project(name="Controles Gantt QA", worker_edit=True)
    Project.objects.filter(pk=project.pk).update(start_date=fixture.today - timedelta(days=60), end_date=None)
    subject = fixture.task(project, name="Calendario controles QA", due_date=str(fixture.today + timedelta(days=400)))
    parent = fixture.task(project, name="Padre controles QA", due_date=str(fixture.today + timedelta(days=60)))
    child = fixture.task(project, name="Hija controles QA", parent_id=parent.pk, state_code="EN_CURSO", due_date=None)
    leaf = fixture.task(project, name="Profunda controles QA", parent_id=child.pk, due_date=None, labels=[{"name": "Revisión controles QA", "kind": "technical"}])
    tail = fixture.task(project, name="Último inicio sin límite QA", due_date=None)
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=parent.pk).update(start_date=fixture.today - timedelta(days=20))
        Task.objects.filter(pk=tail.pk).update(start_date=fixture.today + timedelta(days=120))
        TaskAssignee.objects.create(task=leaf, worker_id=fixture.admin.worker_id)
        TaskAssignee.objects.create(task=leaf, worker_id=fixture.worker.worker_id)
    single = fixture.assigned_project(name="Fecha única Gantt QA")
    one = fixture.task(single, name="Único día controles QA", due_date=str(fixture.today))
    extreme = fixture.assigned_project(name="Siglos controles QA")
    with audit_actor(fixture.admin):
        Project.objects.filter(pk=extreme.pk).update(start_date=date(1900, 1, 1), end_date=None)
        first = Task.objects.create(project=extreme, name="Extremo 1901 controles QA", start_date=date(1901, 1, 1), due_date=None, state_code="PENDIENTE")
        last = Task.objects.create(project=extreme, name="Extremo 2099 controles QA", start_date=date(2099, 12, 31), due_date=date(2099, 12, 31), state_code="EN_CURSO")
    large = fixture.assigned_project(name="2000 tareas controles QA")
    with audit_actor(fixture.admin):
        Task.objects.bulk_create([
            Task(project=large, name=f"Fila controles {index:04} QA", start_date=fixture.today,
                 due_date=None if index % 7 == 0 else fixture.today + timedelta(days=10), state_code="PENDIENTE")
            for index in range(2000)
        ])
        tasks = list(Task.objects.filter(project=large).order_by("pk"))
        TaskOrder.objects.bulk_create([TaskOrder(task=task, position=Decimal(index + 1) * 1024) for index, task in enumerate(tasks)])
    credentials["context"]["controls"] = {
        "project": project.pk, "subject": subject.pk, "parent": parent.pk, "child": child.pk, "leaf": leaf.pk, "tail": tail.pk,
        "single": single.pk, "one": one.pk, "extreme": extreme.pk, "extreme_first": first.pk, "extreme_last": last.pk,
        "large": large.pk, "large_first": tasks[0].pk, "large_last": tasks[-1].pk,
    }
