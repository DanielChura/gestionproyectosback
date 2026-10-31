import json
import os
from pathlib import Path
import secrets
from wsgiref.simple_server import WSGIServer, make_server

import django


class ExclusiveServer(WSGIServer):
    allow_reuse_address = False


def serve():
    if os.environ.get("GM_TASK_DETAIL_BROWSER") != "1":
        raise RuntimeError("Only the isolated task detail browser wrapper may start this fixture")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_test_mysql_templates")
    django.setup()

    from django.core.wsgi import get_wsgi_application
    from django.db import connection
    from apps.authentication.models import AuthThrottle, RecoveryRequest
    from apps.organization.models import Specialty
    from apps.workers.models import WorkerSpecialty
    from apps.projects.models import ProjectMember
    from apps.teams.models import Team, TeamMember
    from django.utils import timezone
    from datetime import timedelta
    from .test_task_detail_mysql import TaskDetailMySQLTests

    TaskDetailMySQLTests.setUpClass()
    fixture = TaskDetailMySQLTests()
    fixture.setUp()
    with connection.schema_editor() as editor:
        for model in (Specialty, WorkerSpecialty, AuthThrottle, RecoveryRequest):
            if model._meta.db_table not in connection.introspection.table_names():
                editor.create_model(model)

    fixture.area.name = "Ingeniería · pruebas aisladas"
    fixture.area.save(update_fields=["name"])
    second = fixture.account("second", "TRABAJADOR", fixture.area)
    for user, name in ((fixture.admin, "Administrador de pruebas"), (fixture.worker, "Ángela QA"), (second, "José QA")):
        user.worker.first_names = name
        user.worker.save(update_fields=["first_names"])
    project = fixture.assigned_project(name="Detalle de tareas · QA", worker_edit=True, worker_contribute=True)
    project.start_date = timezone.localdate() - timedelta(days=14)
    project.save(update_fields=["start_date"])
    ProjectMember.objects.create(project=project, worker=second.worker, active=True)
    specialties = [Specialty.objects.create(name=name) for name in ("Ingeniería civil", "Diseño estructural", "Revisión BIM")]
    teams = [Team.objects.create(area=fixture.area, name=name) for name in ("Estructuras QA", "Revisión QA", "Equipo vacío QA")]
    for team, people in ((teams[0], [fixture.worker, second]), (teams[1], [second])):
        for person in people:
            TeamMember.objects.create(team=team, worker=person.worker, joined_date=timezone.localdate())
    parent = fixture.task(project, name="Revisión de planos", due_date=None, responsible_ids=[fixture.worker.worker_id, second.worker_id])
    updated = fixture.request("patch", f"{project.pk}/tasks/{parent.pk}/", {"estimated_minutes": 60, "priority": 4}, fixture.admin)
    if updated.status_code != 200:
        raise RuntimeError("No se pudo preparar una operación agrupada real")
    child = fixture.task(project, parent_id=parent.pk, name="Verificar estructura", responsible_id=fixture.worker.worker_id, due_date=None)
    fixture.task(project, parent_id=parent.pk, name="Cálculo aprobado", state_code="COMPLETADA", responsible_id=fixture.worker.worker_id, due_date=None)
    related = fixture.task(project, name="Informe técnico", due_date=None)
    reason_task = fixture.task(project, name="Transición con motivo", due_date=None, responsible_id=fixture.worker.worker_id)
    from apps.projects.models import Task
    Task.objects.filter(project=project).update(start_date=project.start_date)
    readonly = fixture.assigned_project(name="Proyecto de solo lectura")
    readonly_task = fixture.task(readonly, name="Consulta sin modificación", responsible_id=fixture.worker.worker_id)
    scrum = fixture.assigned_project(name="Scrum · categoría funcional", worker_edit=True, worker_contribute=True)
    states = fixture.apply(scrum, fixture.preset(scrum, "scrum")).json()["states"]
    scrum_parent = fixture.task(scrum, name="Sprint sintético", state_code=states[0]["code"], due_date=None)
    scrum_child = fixture.task(scrum, parent_id=scrum_parent.pk, name="COMPLETADO aún activo", state_code=states[3]["code"], responsible_id=fixture.worker.worker_id, due_date=None)
    credentials = {}
    from apps.authentication.tokens import issue_stored

    for user, role in ((fixture.admin, "admin"), (fixture.worker, "worker"), (fixture.outsider, "outsider")):
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": password}
        credentials[role]["expired_access"] = issue_stored(user, "access", timezone.now() - timedelta(seconds=30))
    credentials["context"] = {"project": project.pk, "task": parent.pk, "child": child.pk, "related": related.pk,
                              "reason_task": reason_task.pk,
                              "readonly": readonly.pk, "readonly_task": readonly_task.pk, "scrum": scrum.pk,
                              "scrum_task": scrum_parent.pk, "scrum_child": scrum_child.pk}
    credentials["context"].update({"area": fixture.area.pk, "other_area": fixture.other_area.pk, "teams": [team.pk for team in teams], "specialties": [item.pk for item in specialties]})
    if os.environ.get("GM_TASK_LIST_BROWSER") == "1":
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        response = fixture.request("post", f"{project.pk}/tasks/{child.pk}/state/", {"state_code": "EN_CURSO"}, fixture.admin)
        if response.status_code != 200:
            raise RuntimeError("No se pudo preparar el estado independiente")
        grandchild = fixture.task(project, parent_id=child.pk, name="Comprobación de nivel dos", state_code="BLOQUEADA", due_date=None)
        Task.objects.filter(pk=grandchild.pk).update(start_date=parent.start_date)
        date_cases = []
        for name, created, start, due in (
            ("Fecha anterior a creación", 26, 22, 28),
            ("Fecha válida al crear", 20, 22, 22),
            ("Fecha igual a creación", 26, 26, 28),
        ):
            row = fixture.task(project, name=name, due_date=None)
            Task.objects.filter(pk=row.pk).update(created_at=datetime(2026, 10, created, 12, tzinfo=ZoneInfo("America/Lima")), start_date=date(2026, 10, start), due_date=date(2026, 10, due))
            date_cases.append(row.pk)
        credentials["context"].update({"grandchild": grandchild.pk, "date_cases": date_cases})
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")

    print("MySQL temporal y Django reales listos; datos exclusivamente sintéticos.", flush=True)
    try:
        with make_server("127.0.0.1", 8819, get_wsgi_application(), server_class=ExclusiveServer) as server:
            server.serve_forever()
    finally:
        connection.close()


if __name__ == "__main__":
    serve()
