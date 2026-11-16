from datetime import timedelta
from importlib import import_module
import os
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

from apps.projects.models import Task
from apps.projects.services.audit import audit_actor


def prepare(fixture, credentials):
    if os.environ.get("GM_GANTT_DIRECT_BROWSER") != "1" or connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("Direct Gantt QA requires the dedicated loopback database")
    if os.environ.get("GM_GANTT_REGRESSION_BROWSER") == "1":
        from .gantt_regression_browser import prepare as prepare_regressions
        prepare_regressions(fixture, credentials)
    name = "0021_record_dependencies_without_retroactive_validation"
    migration = import_module("apps.projects.migrations." + name)
    state = MigrationLoader(None).project_state([("proyectos", "0019_task_scheduling_dependencies")])
    with patch.dict(os.environ, {"TASK_SCHEDULING_MIGRATION_MAINTENANCE": "1"}):
        MigrationExecutor(connection).apply_migration(state, migration.Migration(name, "proyectos"))
    project = fixture.assigned_project(name="Corrección de dependencias Gantt", worker_edit=True, worker_contribute=True)
    project.start_date = fixture.today - timedelta(days=60)
    project.end_date = fixture.today + timedelta(days=120)
    project.save(update_fields=["start_date", "end_date"])

    def task(name, first, last, **kwargs):
        return fixture.task(project, name=name, start_date=str(fixture.today + timedelta(days=first)),
                            due_date=str(fixture.today + timedelta(days=last)) if last is not None else None, **kwargs)

    source = task("Origen directo", 3, 5)
    ids = {"source": source.pk}
    for name, first, last in [("FS", 8, 10), ("SS", 3, 6), ("FF", 3, 8), ("SF", 0, 2), ("body", 1, 8), ("adjust", 1, 8)]:
        ids[name] = task("Destino " + name, first, last).pk
    closed = task("Completada histórica", 0, 4, state_code="COMPLETADA")
    unplanned = task("Cerrada sin barra", 0, None, state_code="COMPLETADA")
    parent = task("Padre completado", 0, 50)
    child = task("Subtarea histórica sin barra", 0, None, parent_id=parent.pk, state_code="COMPLETADA")
    response = fixture.request("post", f"{project.pk}/tasks/{parent.pk}/state/", {"state_code": "COMPLETADA"}, fixture.admin)
    if response.status_code != 200:
        raise RuntimeError("Unable to prepare the completed parent")
    with audit_actor(fixture.admin):
        Task.objects.filter(pk__in=[closed.pk, unplanned.pk, child.pk, parent.pk]).update(actual_started_at=None, actual_finished_at=None)
    ids.update(closed=closed.pk, unplanned=unplanned.pk, parent=parent.pk, child=child.pk)
    for index in range(30):
        row = task(f"Fila inferior {index:02}", 0, None)
        if index == 29:
            ids["last"] = row.pk
    credentials["direct"] = {"project": project.pk, "today": str(fixture.today), **ids}
