import json
import os
import secrets
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import django
from waitress import serve


def prepare_browser_clients(credentials):
    from django.db import connection
    from django.test import Client
    from django.utils import timezone
    from apps.authentication.models import User
    from apps.organization.models import Area, Specialty
    from apps.projects.models import Project, ProjectMember
    from .test_state_policy_integration_mysql import StatePolicyIntegrationMySQLTests

    if os.environ.get("GM_VIEWS_BROWSER") != "1" or connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Requires the synthetic loopback browser database")
    fixture = StatePolicyIntegrationMySQLTests()
    fixture.area = Area.objects.get(pk=credentials["context"]["area"])
    fixture.admin = User.objects.get(worker__email=credentials["admin"]["email"])
    fixture.worker = User.objects.get(worker__email=credentials["worker"]["email"])
    fixture.today = timezone.localdate()
    fixture.client = Client(enforce_csrf_checks=True)
    fixture.tokens = {}
    if "admin2" not in credentials:
        user = fixture.account("admin_second_qa", "ADMINISTRADOR", fixture.area)
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials["admin2"] = {"email": user.worker.email, "password": password}
    Specialty.objects.get_or_create(name="Especialidad de vistas QA")
    second = User.objects.get(worker__email=credentials["admin2"]["email"])
    ProjectMember.objects.get_or_create(project_id=credentials["context"]["project"], worker_id=second.worker_id, defaults={"active": True})
    for index in range(56):
        name = f"Directorio QA {index:03}"
        if not Project.objects.filter(name=name).exists():
            project = fixture.assigned_project(name=name)
            credentials["context"]["directory_last"] = project.pk
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")


def main():
    if os.environ.get("GM_VIEWS_BROWSER") != "1" or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_templates":
        raise RuntimeError("Requires isolated views browser settings")
    django.setup()
    from django.conf import settings
    from django.core.wsgi import get_wsgi_application
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.loader import MigrationLoader
    from apps.authentication.models import AuthThrottle, Invitation, RecoveryRequest
    from apps.projects.models import Task
    from apps.projects.services.audit import audit_actor
    from apps.publications.models import ProjectPublication, PublicationAssignment, PublicationRequirement
    from apps.teams.models import Team, TeamMember
    from apps.workers.models import WorkerSpecialty
    from .test_state_policy_integration_mysql import StatePolicyIntegrationMySQLTests

    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Requires an empty synthetic loopback database")
    if os.environ.get("GM_VIEWS_REUSE") == "1":
        credentials = json.loads(Path(os.environ["GM_QA_SECRETS_PATH"]).read_text(encoding="utf-8"))
        if not Task.objects.filter(project_id=credentials["context"]["project"]).exists():
            raise RuntimeError("The existing synthetic fixture was not found")
        prepare_browser_clients(credentials)
        print("Existing synthetic MySQL views fixture ready", flush=True)
        serve(get_wsgi_application(), host="127.0.0.1", port=8819, threads=6)
        return
    if connection.introspection.table_names():
        raise RuntimeError("The synthetic database must be empty")
    settings.DATABASES["default"]["CONN_MAX_AGE"] = None
    settings.DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
    StatePolicyIntegrationMySQLTests.setUpClass()
    with connection.cursor() as cursor:
        columns = [column.name for column in connection.introspection.get_table_description(cursor, "actividad")]
        if "calendario_revision" in columns:
            cursor.execute("ALTER TABLE actividad DROP COLUMN calendario_revision")
    for number, previous, maintenance in (
        ("0017_task_calendar_guard", "0016_task_state_policy_consistency", "TASK_CALENDAR_MIGRATION_MAINTENANCE"),
        ("0018_tema_remove_projecttaskstatus_color_and_more", "0017_task_calendar_guard", "TASK_TEMA_MIGRATION_MAINTENANCE"),
    ):
        module = import_module("apps.projects.migrations." + number)
        state = MigrationLoader(None).project_state([("proyectos", previous)])
        with patch.dict(os.environ, {maintenance: "1"}):
            MigrationExecutor(connection).apply_migration(state, module.Migration(number, "proyectos"))
    fixture = StatePolicyIntegrationMySQLTests()
    fixture.setUp()
    with connection.schema_editor() as editor:
        for model in (WorkerSpecialty, AuthThrottle, Invitation, RecoveryRequest, ProjectPublication, PublicationRequirement, PublicationAssignment):
            if model._meta.db_table not in connection.introspection.table_names():
                editor.create_model(model)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE ausencia (id BIGINT PRIMARY KEY, trabajador_id BIGINT NOT NULL, anulada BOOLEAN NOT NULL DEFAULT 0, fecha_inicio DATE NOT NULL, fecha_fin DATE NOT NULL)")
        cursor.execute((Path(__file__).resolve().parents[2] / "authentication/migrations/sql/v_disponibilidad_actual.sql").read_text(encoding="utf-8"))
    project = fixture.assigned_project(name="Recuperación de vistas QA", worker_edit=True, worker_contribute=True)
    project.start_date = fixture.today - timedelta(days=40)
    project.end_date = fixture.today + timedelta(days=90)
    project.save(update_fields=["start_date", "end_date"])
    parent = fixture.task(project, name="Raíz histórica QA", responsible_id=fixture.worker.worker_id)
    child = fixture.task(project, name="Subtarea independiente QA", parent_id=parent.pk, state_code="EN_CURSO")
    nested = fixture.task(project, name="Tercer nivel QA", parent_id=child.pk)
    single = fixture.task(project, name="Un día QA", due_date=str(fixture.today))
    other = fixture.task(project, name="Sin fecha límite QA")
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=parent.pk).update(start_date=fixture.today - timedelta(days=20), due_date=fixture.today + timedelta(days=60))
        Task.objects.filter(pk=child.pk).update(due_date=fixture.today + timedelta(days=30))
    team = Team.objects.create(area=fixture.area, name="Equipo de vistas QA", active=True)
    TeamMember.objects.create(team=team, worker=fixture.worker.worker, joined_date=fixture.today)
    readonly = fixture.assigned_project(name="Lectura QA")
    fixture.task(readonly, name="Solo lectura QA")
    credentials = {}
    for user, role in ((fixture.admin, "admin"), (fixture.worker, "worker"), (fixture.outsider, "outsider")):
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": password}
    credentials["context"] = {"area": fixture.area.pk, "other_area": fixture.other_area.pk, "project": project.pk, "readonly": readonly.pk, "parent": parent.pk, "child": child.pk, "nested": nested.pk, "single": single.pk, "other": other.pk, "team": team.pk, "worker": fixture.worker.worker_id, "today": str(fixture.today)}
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")
    prepare_browser_clients(credentials)
    print("Isolated MySQL schema through 0018 and synthetic views fixtures ready", flush=True)
    serve(get_wsgi_application(), host="127.0.0.1", port=8819, threads=6)


if __name__ == "__main__":
    main()

