import os
import django
django.setup()
from importlib import import_module
from unittest.mock import patch
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from apps.projects.tests.test_calendar_mysql import CalendarMySQLTests
from apps.projects.tests.test_state_policy_integration_mysql import StatePolicyIntegrationMySQLTests

def current_schema(cls):
    if connection.settings_dict["HOST"] != "127.0.0.1" or connection.settings_dict["NAME"] != "gm_test_task_status_templates":
        raise RuntimeError("QA loopback database required")
    StatePolicyIntegrationMySQLTests.setUpClass.__func__(cls)
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE actividad DROP COLUMN calendario_revision")
    for previous, name, flag in [
        ("0016_task_state_policy_consistency", "0017_task_calendar_guard", "TASK_CALENDAR_MIGRATION_MAINTENANCE"),
        ("0017_task_calendar_guard", "0018_tema_remove_projecttaskstatus_color_and_more", "TASK_TEMA_MIGRATION_MAINTENANCE"),
    ]:
        migration = import_module("apps.projects.migrations." + name)
        state = MigrationLoader(None).project_state([("proyectos", previous)])
        with patch.dict(os.environ, {flag: "1"}):
            MigrationExecutor(connection).apply_migration(state, migration.Migration(name, "proyectos"))
    print("Isolated QA schema prepared through current 0018; historical fixture seed was before theme schema.", flush=True)

CalendarMySQLTests.setUpClass = classmethod(current_schema)


from apps.projects.tests.gantt_browser_server import prepare
from apps.projects.tests.remote_integration_browser_server import serve

def seed(fixture, credentials):
    prepare(fixture, credentials)
    from apps.projects.models import Project, TaskDependency
    from datetime import timedelta
    project=fixture.assigned_project(name="Programación horaria QA", worker_edit=True, worker_contribute=True)
    old_a=fixture.task(project,name="Legado origen QA")
    old_b=fixture.task(project,name="Legado destino QA")
    TaskDependency.objects.create(project=project,predecessor=old_a,successor=old_b)
    with connection.cursor() as cursor:
        for name in ("precision_calendario", "hora_inicio_planificada", "hora_fin_planificada", "inicio_real_en", "fin_real_en"):
            cursor.execute(f"ALTER TABLE actividad DROP COLUMN {name}")
        cursor.execute("ALTER TABLE dependencia_actividad DROP COLUMN tipo")
    migration=import_module("apps.projects.migrations.0019_task_scheduling_dependencies")
    state=MigrationLoader(None).project_state([("proyectos","0018_tema_remove_projecttaskstatus_color_and_more")])
    with patch.dict(os.environ,{"TASK_SCHEDULING_MIGRATION_MAINTENANCE":"1"}):
        MigrationExecutor(connection).apply_migration(state,migration.Migration("0019_task_scheduling_dependencies","proyectos"))
    ids=[]
    day=str(fixture.today+timedelta(days=1))
    for name,start,end in [("Hora origen QA",9,10),("Hora destino QA",11,12),("Hora sin límite QA",13,None)]:
        payload=fixture.task_payload(name=name)
        payload.pop("due_date",None)
        payload["schedule"]={"precision":"hour","start":f"{day}T{start:02d}:00:00-05:00","end":f"{day}T{end:02d}:00:00-05:00" if end else None,"timezone":"America/Lima"}
        response=fixture.request("post",f"{project.pk}/tasks/",payload,fixture.admin)
        if response.status_code!=201: raise RuntimeError(response.content)
        ids.append(response.json()["id"])
    credentials["scheduling"]={"project":project.pk,"source":ids[0],"target":ids[1],"unbounded":ids[2],"legacy_source":old_a.pk,"legacy_target":old_b.pk,"day":day}
    if os.environ.get("GM_GANTT_DIRECT_BROWSER") == "1":
        from .gantt_direct_browser import prepare as prepare_direct
        prepare_direct(fixture, credentials)
    print("Scheduling QA through 0019 ready; only synthetic records.",flush=True)

serve(seed)
