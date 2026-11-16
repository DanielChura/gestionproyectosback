from datetime import timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from django.db import connection
from apps.projects.models import Project, Task
from apps.projects.services.audit import audit_actor
from .gantt_closure_fixture import closure_snapshot, require_closure_database


def prepare_timeline(fixture, credentials):
    require_closure_database()
    project = fixture.assigned_project(name="Barras y conexiones QA", worker_edit=True)
    with audit_actor(fixture.admin):
        Project.objects.filter(pk=project.pk).update(start_date=fixture.today-timedelta(days=60), end_date=None)
    project.refresh_from_db()
    end = lambda days: str(fixture.today + timedelta(days=days))
    a = fixture.task(project, name="Predecesora temporal QA", due_date=end(4))
    b = fixture.task(project, name="Sucesora temporal con responsable QA", start_date=end(9), due_date=end(12), responsible_id=fixture.worker.worker_id, state_code="EN_CURSO")
    c = fixture.task(project, name="Destino lejano QA", start_date=end(18), due_date=end(22))
    missing = fixture.task(project, name="Raíz sin límite temporal QA", due_date=None)
    child = fixture.task(project, name="Subtarea sin límite temporal QA", parent_id=a.pk, due_date=None)
    single = fixture.task(project, name="Un solo día temporal QA", start_date=end(6), due_date=end(6))
    outside = fixture.task(project, name="Fechada fuera del viewport QA", start_date=end(400), due_date=end(410))
    unbound = fixture.task(project, name="Indicador protegido sin límite QA", due_date=None)
    readonly = fixture.assigned_project(name="Consulta temporal QA", worker_edit=False, worker_state=False)
    readonly_task = fixture.task(readonly, name="Solo consulta sin límite QA", due_date=None, responsible_id=fixture.worker.worker_id)
    historic = fixture.task(project, name="Histórica temporal QA", due_date=None)
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=historic.pk).update(start_date=fixture.today-timedelta(days=25), due_date=fixture.today-timedelta(days=20))
    checks = []

    def dependency(source, target, user=None):
        return fixture.request("post", f"{project.pk}/tasks/{target.pk}/dependencies/", {"predecessor_id": source.pk}, user or fixture.admin)

    if dependency(a, b).status_code != 201:
        raise RuntimeError("Initial dependency failed")
    for source, target, status in ((a, b, 400), (b, a, 400), (a, a, 400), (a, child, 400)):
        if dependency(source, target).status_code != status:
            raise RuntimeError("Dependency rule changed")
    if dependency(a, c, fixture.worker).status_code not in (403, 404):
        raise RuntimeError("Worker dependency accepted")
    foreign = fixture.task(fixture.assigned_project(name="Otro proyecto temporal QA"))
    if dependency(foreign, c).status_code != 404:
        raise RuntimeError("Cross project dependency accepted")
    current = fixture.request("get", f"{project.pk}/tasks/{a.pk}/", user=fixture.admin).json()["task"]
    invalid = fixture.request("patch", f"{project.pk}/tasks/{a.pk}/", {"start_date": None, "expected_calendar_revision": current["calendar_revision"], "calendar_intent_id": str(uuid4())}, fixture.admin)
    if invalid.status_code != 400:
        raise RuntimeError("Required calendar start changed")
    with connection.cursor() as cursor:
        cursor.execute("SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='actividad' AND COLUMN_NAME='fecha_inicio'")
        nullable = cursor.fetchone()[0]
    if nullable != "NO":
        raise RuntimeError("Unexpected calendar schema")
    checks.extend(["Duplicate, cycle, self, subtask, cross-project and worker dependency rejection", "Required start date validated by API and MySQL NOT NULL"])
    credentials["context"]["timeline"] = dict(project=project.pk, a=a.pk, b=b.pk, c=c.pk, missing=missing.pk, child=child.pk, single=single.pk, outside=outside.pk, historic=historic.pk, unbound=unbound.pk, readonly_project=readonly.pk, readonly_task=readonly_task.pk, day=end(0), day1=end(1), day2=end(2), day3=end(3), day4=end(4), day30=end(30))
    output = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    (output / "timeline-api.json").write_text(json.dumps({"checks": checks, "start_nullable": nullable}, indent=2), encoding="utf-8")
    (output / "content-baseline.json").write_text(json.dumps(closure_snapshot(), default=str, indent=2), encoding="utf-8")
