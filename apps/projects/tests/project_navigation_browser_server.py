import json
import os
import secrets
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

import django


class NavigationServer(ThreadingMixIn, WSGIServer):
    allow_reuse_address = False
    daemon_threads = True


def run():
    if os.environ.get("GM_PROJECT_NAVIGATION_BROWSER") != "1" or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_templates":
        raise RuntimeError("Requires the isolated project navigation wrapper")
    django.setup()
    from django.core.wsgi import get_wsgi_application
    from django.db import connection, transaction
    from apps.authentication.models import AuthThrottle, RecoveryRequest
    from apps.organization.models import Area, Specialty
    from apps.projects.models import Project, ProjectMember
    from apps.workers.models import WorkerArea, WorkerSpecialty
    from .test_calendar_mysql import CalendarMySQLTests

    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Expected the new synthetic loopback database")
    if len(sys.argv) > 1:
        action, identifier = sys.argv[1], int(sys.argv[2])
        if action not in ("rename", "archive"):
            raise RuntimeError("Unsupported QA action")
        with transaction.atomic():
            project = Project.objects.select_for_update().get(pk=identifier, code="NAV-OLD")
            if action == "rename":
                project.name = "Proyecto renombrado QA"
                project.save(update_fields=["name"])
            else:
                project.archived = True
                project.save(update_fields=["archived"])
        connection.close()
        print("Controlled synthetic fixture update completed.")
        return
    CalendarMySQLTests.setUpClass()
    fixture = CalendarMySQLTests()
    fixture.setUp()
    with connection.schema_editor() as editor:
        for model in (Specialty, WorkerSpecialty, AuthThrottle, RecoveryRequest):
            if model._meta.db_table not in connection.introspection.table_names():
                editor.create_model(model)
    area = fixture.area
    area.name = "Desarrollo de Software QA"
    area.save(update_fields=["name"])
    old_area = Area.objects.create(code="NAV_OLD", name="Obras y planificación QA", active=True)
    WorkerArea.objects.create(worker=fixture.worker.worker, area=area)
    WorkerArea.objects.create(worker=fixture.worker.worker, area=old_area)
    WorkerArea.objects.create(worker=fixture.outsider.worker, area=fixture.other_area)
    old = Project.objects.create(code="NAV-OLD", name="Proyecto antiguo QA", area=old_area,
        type_code="GENERAL", start_date=fixture.today, available=False)
    ProjectMember.objects.create(project=old, worker=fixture.worker.worker)
    Project.objects.bulk_create([Project(code=f"NAV-MAIN-{i}", name=f"Proyecto QA {i:03}", area=area,
        type_code="GENERAL", start_date=fixture.today, available=True, worker_create=True) for i in range(235)])
    main = list(Project.objects.filter(code__startswith="NAV-MAIN-").order_by("pk"))
    ProjectMember.objects.bulk_create([ProjectMember(project=project, worker=fixture.worker.worker) for project in main[:-2]])
    extras = []
    for i in range(32):
        extra = Area.objects.create(code=f"NAV_EXTRA_{i}", name=f"Área técnica QA {i:02}", active=True)
        WorkerArea.objects.create(worker=fixture.outsider.worker, area=extra)
        project = Project.objects.create(code=f"NAV-EXTRA-{i}", name=f"Proyecto técnico QA {i:02}", area=extra,
            type_code="GENERAL", start_date=fixture.today, available=False)
        ProjectMember.objects.create(project=project, worker=fixture.outsider.worker)
        extras.append({"area_id": extra.pk, "area_name": extra.name, "project_id": project.pk})
    hidden = Project.objects.create(code="NAV-HIDDEN", name="Proyecto restringido QA", area=fixture.other_area,
        type_code="GENERAL", start_date=fixture.today, available=False)
    ProjectMember.objects.create(project=hidden, worker=fixture.outsider.worker)
    credentials = {}
    for user, role in ((fixture.admin, "admin"), (fixture.worker, "worker")):
        password = secrets.token_urlsafe(24)
        user.set_password(password)
        user.save(update_fields=["password"])
        credentials[role] = {"email": user.worker.email, "password": password}
    credentials["context"] = {"area": area.pk, "area_name": area.name, "old_area": old_area.pk, "old_area_name": old_area.name,
        "old_project": old.pk, "first_project": main[0].pk, "unassociated_project": main[-1].pk, "hidden_project": hidden.pk,
        "worker_id": fixture.worker.worker_id, "extras": extras, "project_count": Project.objects.count()}
    Path(os.environ["GM_QA_SECRETS_PATH"]).write_text(json.dumps(credentials), encoding="utf-8")
    print("Project navigation QA: real Django, new MySQL 8.4, current migrations ready.", flush=True)
    try:
        with make_server("127.0.0.1", 8819, get_wsgi_application(), server_class=NavigationServer) as server:
            server.serve_forever()
    finally:
        connection.close()


if __name__ == "__main__":
    run()
