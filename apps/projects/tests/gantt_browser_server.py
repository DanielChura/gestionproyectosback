from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import secrets

from .remote_integration_browser_server import serve


def domain_snapshot():
    import hashlib
    from django.db import connection
    from apps.projects.models import Task, TaskActivity, TaskOrder, TaskDependency

    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Domain inspection requires the isolated Gantt database")
    result = {}
    for model in (Task, TaskActivity, TaskOrder, TaskDependency):
        rows = list(model.objects.order_by("pk").values())
        result[model._meta.db_table] = {
            "count": len(rows),
            "sha256": hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest(),
        }
    return result


def prepare(fixture, credentials):
    if os.environ.get("GM_GANTT_BROWSER") != "1":
        raise RuntimeError("Gantt data may only be prepared by its isolated QA wrapper")
    from apps.projects.models import Project, Task
    from apps.projects.services.audit import audit_actor

    project = Project.objects.get(pk=credentials["context"]["project"])
    one_day = fixture.task(project, name="Un día Gantt QA", due_date=str(fixture.today))
    historical = fixture.task(project, name="Histórica Gantt QA")
    unbounded = fixture.task(project, name="Sin límite Gantt QA", due_date=None)
    child = fixture.task(project, name="Subtarea sin límite Gantt QA", parent_id=unbounded.pk, due_date=None, state_code="EN_CURSO")
    dated_child = fixture.task(project, name="Subtarea fechada Gantt QA", parent_id=unbounded.pk, due_date=str(fixture.today + timedelta(days=2)))
    orphan = fixture.task(project, name="Subtarea autorizada Gantt QA", parent_id=unbounded.pk, due_date=None, responsible_id=fixture.worker.worker_id)
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=historical.pk).update(
            start_date=fixture.today - timedelta(days=7), due_date=fixture.today - timedelta(days=7),
        )
    empty_project = fixture.assigned_project(name="Gantt vacío QA")
    null_project = fixture.assigned_project(name="Gantt sin límites QA")
    null_parent = fixture.task(null_project, name="Padre abierto Gantt QA", due_date=None)
    null_child = fixture.task(null_project, name="Hijo abierto Gantt QA", due_date=None, parent_id=null_parent.pk)
    readonly_project = fixture.assigned_project(name="Gantt consulta QA")
    readonly_task = fixture.task(readonly_project, name="Tarea consulta Gantt QA", responsible_id=fixture.worker.worker_id)
    password = secrets.token_urlsafe(24)
    fixture.outsider.set_password(password)
    fixture.outsider.save(update_fields=["password"])
    credentials["outsider"] = {"email": fixture.outsider.worker.email, "password": password}
    for role in ("admin_delay", "admin_chunk"):
        user = fixture.account("gantt_" + role, "ADMINISTRADOR", fixture.area)
        secret = secrets.token_urlsafe(24)
        user.set_password(secret)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": secret}
    credentials["context"].update(
        one_day=one_day.pk, historical=historical.pk, unbounded=unbounded.pk, null_child=child.pk,
        dated_child=dated_child.pk, orphan=orphan.pk, empty_project=empty_project.pk,
        null_project=null_project.pk, null_parent=null_parent.pk, all_null_child=null_child.pk,
        readonly_project=readonly_project.pk, readonly_task=readonly_task.pk,
    )

    if os.environ.get("GM_GANTT_STAGE") == "2":
        from .gantt_operational_fixture import prepare_operational
        prepare_operational(fixture, credentials)

    Path(os.environ["GM_BROWSER_REPORT_DIR"], "baseline_domain.json").write_text(
        json.dumps(domain_snapshot(), indent=2), encoding="utf-8",
    )


if __name__ == "__main__":
    if os.environ.get("GM_GANTT_BROWSER") != "1":
        raise RuntimeError("Requires the isolated Gantt browser wrapper")
    if sys.argv[1:] == ["--verify-domain"]:
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_templates":
            raise RuntimeError("Requires isolated MySQL settings")
        import django
        django.setup()
        target = Path(os.environ["GM_BROWSER_REPORT_DIR"])
        expected = json.loads((target / "baseline_domain.json").read_text(encoding="utf-8"))
        actual = domain_snapshot()
        (target / "final_domain.json").write_text(json.dumps(actual, indent=2), encoding="utf-8")
        if actual != expected:
            raise RuntimeError("Gantt browser checks changed confirmed domain data")
        print("Task rows, calendars, states, order and complete history unchanged.", flush=True)
    else:
        serve(prepare)
