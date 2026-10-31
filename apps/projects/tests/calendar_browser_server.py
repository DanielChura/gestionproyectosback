import json
import os
from pathlib import Path
import secrets
from datetime import timedelta
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

import django


class CalendarServer(ThreadingMixIn, WSGIServer):
    allow_reuse_address = False
    daemon_threads = True


def serve():
    if os.environ.get("GM_CALENDAR_BROWSER") != "1" or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_templates":
        raise RuntimeError("Only the isolated calendar browser wrapper may start this fixture")
    django.setup()
    from django.core.wsgi import get_wsgi_application
    from django.db import connection
    from django.utils import timezone
    from apps.authentication.models import AuthThrottle, RecoveryRequest
    from apps.organization.models import Specialty
    from apps.workers.models import WorkerSpecialty
    from apps.projects.models import Task
    from apps.projects.services.audit import audit_actor
    from .test_calendar_mysql import CalendarMySQLTests

    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Expected a loopback dedicated MySQL database")
    CalendarMySQLTests.setUpClass()
    fixture = CalendarMySQLTests()
    fixture.setUp()
    with connection.schema_editor() as editor:
        for model in (Specialty, WorkerSpecialty, AuthThrottle, RecoveryRequest):
            if model._meta.db_table not in connection.introspection.table_names():
                editor.create_model(model)
    today = timezone.localdate()
    project = fixture.assigned_project(name="Calendario seguro QA", worker_edit=True, worker_contribute=True)
    project.start_date = today - timedelta(days=30)
    project.end_date = today + timedelta(days=60)
    project.save(update_fields=["start_date", "end_date"])
    subject = fixture.task(project, name="Ventana concurrente QA", responsible_id=fixture.worker.worker_id, due_date=str(today + timedelta(days=25)))
    parent = fixture.task(project, name="Padre de calendario QA", due_date=str(today + timedelta(days=20)))
    child = fixture.task(project, name="Subtarea de calendario QA", parent_id=parent.pk, due_date=str(today + timedelta(days=10)), responsible_id=fixture.worker.worker_id)
    second = fixture.assigned_project(name="Segundo calendario QA", worker_edit=True)
    second_task = fixture.task(second, name="Tarea de segundo proyecto QA")
    readonly = fixture.assigned_project(name="Calendario solo lectura QA")
    readonly_task = fixture.task(readonly, name="Consulta de calendario QA", responsible_id=fixture.worker.worker_id)
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=subject.pk).update(start_date=today - timedelta(days=10), created_at=timezone.now() - timedelta(days=5))
        Task.objects.filter(pk=parent.pk).update(start_date=today - timedelta(days=10))
        Task.objects.filter(pk=child.pk).update(start_date=today - timedelta(days=5))
    second_admin = fixture.account("admin_calendar_b", "ADMINISTRADOR", fixture.other_area)
    credentials = {}
    for user, role in ((fixture.admin, "admin"), (second_admin, "admin_b"), (fixture.worker, "worker"), (fixture.outsider, "outsider")):
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": password}
    credentials["context"] = {"project": project.pk, "subject": subject.pk, "parent": parent.pk, "child": child.pk,
        "second_project": second.pk, "second_task": second_task.pk, "readonly": readonly.pk, "readonly_task": readonly_task.pk,
        "today": today.isoformat(), "project_start": project.start_date.isoformat(), "project_end": project.end_date.isoformat()}
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")
    print("Calendar QA: real Django and isolated MySQL 8.4 with migration 0017 ready.", flush=True)
    try:
        with make_server("127.0.0.1", 8819, get_wsgi_application(), server_class=CalendarServer) as server:
            server.serve_forever()
    finally:
        connection.close()


if __name__ == "__main__":
    serve()
