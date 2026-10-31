import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from waitress import serve as serve_wsgi

import django



def serve():
    if os.environ.get("GM_CALENDAR_BROWSER") != "1" or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_templates":
        raise RuntimeError("Requires the isolated calendar browser wrapper")
    django.setup()
    from django.conf import settings
    from django.core.wsgi import get_wsgi_application
    from django.db import connection
    from apps.authentication.models import AuthThrottle, Invitation, RecoveryRequest
    from apps.organization.models import Specialty
    from apps.projects.models import ProjectMember
    from apps.publications.models import ProjectPublication, PublicationAssignment, PublicationRequirement
    from apps.teams.models import Team, TeamMember
    from apps.workers.models import WorkerSpecialty
    from .test_calendar_mysql import CalendarMySQLTests

    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Expected a new loopback database")
    settings.DATABASES["default"]["CONN_MAX_AGE"] = None
    settings.DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
    CalendarMySQLTests.setUpClass()
    fixture = CalendarMySQLTests()
    fixture.setUp()
    with connection.schema_editor() as editor:
        for model in (Specialty, WorkerSpecialty, AuthThrottle, Invitation, RecoveryRequest, ProjectPublication, PublicationRequirement, PublicationAssignment):
            if model._meta.db_table not in connection.introspection.table_names():
                editor.create_model(model)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE ausencia (id BIGINT PRIMARY KEY, trabajador_id BIGINT NOT NULL, anulada BOOLEAN NOT NULL DEFAULT 0, fecha_inicio DATE NOT NULL, fecha_fin DATE NOT NULL)")
        cursor.execute((Path(__file__).resolve().parents[2] / "authentication/migrations/sql/v_disponibilidad_actual.sql").read_text(encoding="utf-8"))
    project = fixture.assigned_project(name="Integración remota QA", worker_edit=True, worker_contribute=True)
    project.start_date = fixture.today - timedelta(days=20)
    project.save(update_fields=["start_date"])
    first = fixture.task(project, name="Arrastre continuo QA", responsible_id=fixture.worker.worker_id, labels=[{"name": "QA", "kind": "technical"}])
    second = fixture.task(project, name="Otra tarea QA")
    parent = fixture.task(project, name="Padre QA")
    child = fixture.task(project, name="Subtarea independiente QA", parent_id=parent.pk, state_code="EN_CURSO")
    specialty = Specialty.objects.create(name="Competencia remota QA")
    owned = Specialty.objects.create(name="Competencia previa QA")
    WorkerSpecialty.objects.create(worker=fixture.worker.worker, specialty=owned, level=1)
    team = Team.objects.create(area=fixture.area, name="Equipo remoto QA", active=True)
    TeamMember.objects.create(team=team, worker=fixture.worker.worker, joined_date=fixture.today)
    ProjectMember.objects.filter(project=project, worker=fixture.worker.worker).update(active=True)
    candidate_code = None
    for index in range(62):
        user = fixture.account(f"integration{index:03}", "TRABAJADOR", fixture.area)
        RecoveryRequest.objects.create(user=user)
        if index == 0:
            candidate_code = user.worker.code
    credentials = {}
    for user, role in ((fixture.admin, "admin"), (fixture.worker, "worker")):
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": password}
    credentials["context"] = {"area": fixture.area.pk, "candidate_code": candidate_code, "project": project.pk, "first": first.pk, "second": second.pk,
        "parent": parent.pk, "child": child.pk, "specialty": specialty.pk, "team": team.pk, "today": str(fixture.today)}
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")
    print("Current application, migration 0017, synthetic publications and pagination fixtures ready.", flush=True)
    try:
        serve_wsgi(get_wsgi_application(), host="127.0.0.1", port=8819, threads=4)
    finally:
        connection.close()


if __name__ == "__main__":
    serve()
