from datetime import timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from django.db import DatabaseError, connection

from apps.projects.models import Task, TaskOrder
from apps.projects.services.audit import audit_actor


def prepare(fixture, credentials):
    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Regression fixtures require the dedicated loopback database")
    project = fixture.assigned_project(name="Primer Proyecto sintético", worker_edit=True, worker_contribute=True)
    project.start_date = fixture.today - timedelta(days=39)
    project.end_date = fixture.today + timedelta(days=22)
    project.save(update_fields=["start_date", "end_date"])

    def task(name, start, end, state="PENDIENTE", parent=None):
        row = fixture.task(project, name=name, start_date=str(fixture.today),
                           due_date=str(fixture.today + timedelta(days=max(0, end))) if end is not None else None,
                           state_code=state, parent_id=parent)
        result = fixture.request("patch", f"{project.pk}/tasks/{row.pk}/",
                                 {"start_date": str(fixture.today + timedelta(days=start)),
                                  "due_date": str(fixture.today + timedelta(days=end)) if end is not None else None,
                                  "expected_calendar_revision": str(row.calendar_revision), "calendar_intent_id": str(uuid4())}, fixture.admin)
        if result.status_code != 200:
            raise RuntimeError(result.content)
        row.refresh_from_db()
        return row

    fifth = task("Tarea Quinta", -27, 0, "EN_CURSO")
    second = task("Segunda tarea", -29, 14, "EN_CURSO")
    purple = task("Primer Actividad Proyecto de Software", -39, 22, "EN_REVISION")
    activity = task("Actividad 1.1", -24, None, "EN_CURSO", purple.pk)
    hello = task("Hola", -39, None, parent=purple.pk)
    child = task("Actividad 11", -39, None, "BLOQUEADA", hello.pk)
    result = fixture.request("post", f"{project.pk}/tasks/{hello.pk}/state/", {"state_code": "COMPLETADA"}, fixture.admin)
    if result.status_code != 200:
        raise RuntimeError("Unable to prepare the historical closed parent")
    green = task("Septima Tarea", -39, -24, "COMPLETADA")
    third = task("Tercera tarea", -24, None, "BLOQUEADA")
    sixth = task("Sexta tarea", -24, None, "COMPLETADA")
    pending = task("asdasd", -4, 2)
    fourth = task("Cuarta tarea", -24, None, "COMPLETADA")
    review = task("Tarea de prueba", -3, 0, "EN_REVISION")
    last = task("Actualización de vista de Publicaciones", -10, 14, "EN_CURSO")
    with audit_actor(fixture.admin):
        Task.objects.filter(project=project).update(actual_started_at=None, actual_finished_at=None)
    for index, row in enumerate([fifth, second, purple, activity, hello, child, green, third, sixth, pending, fourth, review, last]):
        TaskOrder.objects.update_or_create(task=row, defaults={"position": index * 100})
    failures = []

    def trace(execute, sql, params, many, context):
        try:
            return execute(sql, params, many, context)
        except DatabaseError as failure:
            failures.append(str(failure))
            raise

    baseline = {}
    with connection.execute_wrapper(trace):
        for kind in ("SS", "SF"):
            result = fixture.request("post", f"{project.pk}/tasks/{green.pk}/dependencies/",
                                     {"predecessor_id": purple.pk, "dependency_type": kind}, fixture.admin)
            baseline[kind] = {"status": result.status_code, "body": result.json()}
        hello.refresh_from_db()
        result = fixture.request("patch", f"{project.pk}/tasks/{hello.pk}/",
                                 {"start_date": str(fixture.today - timedelta(days=25)), "due_date": str(fixture.today),
                                  "expected_calendar_revision": str(hello.calendar_revision), "calendar_intent_id": str(uuid4())}, fixture.admin)
        baseline["Hola"] = {"status": result.status_code, "body": result.json()}
    baseline["sql_errors"] = failures
    Path(os.environ["GM_BROWSER_REPORT_DIR"], "regression-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    credentials["regressions"] = {"project": project.pk, "purple": purple.pk, "green": green.pk,
                                  "hello": hello.pk, "child": child.pk, "last": last.pk, "review": review.pk,
                                  "pending": pending.pk, "today": str(fixture.today)}
