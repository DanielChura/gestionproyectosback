from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
import json
import os
from threading import Event
import time
from unittest.mock import patch

from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.utils import timezone

from apps.authentication.tokens import issue_access
from apps.projects.models import Task, TaskActivity, TaskDependency, TaskCalendarIntent
from .test_scheduling import SchedulingContractTests
from .test_state_policy_integration_mysql import StatePolicyIntegrationMySQLTests as Fixtures


class SchedulingMySQLTests(SchedulingContractTests):
    legacy = False
    @classmethod
    def setUpClass(cls):
        Fixtures.setUpClass.__func__(cls)
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
        fixture = cls()
        Fixtures.setUp(fixture)
        project = fixture.project()
        a = fixture.task(project)
        b = fixture.task(project)
        TaskDependency.objects.create(project=project, predecessor=a, successor=b)
        Task.objects.filter(pk=a.pk).update(state_code="EN_CURSO")
        Task.objects.filter(pk=b.pk).update(state_code="COMPLETADA")
        with connection.cursor() as cursor:
            cursor.execute("SELECT id,fecha_inicio,fecha_fin,estado_codigo,calendario_revision FROM actividad ORDER BY id")
            old = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) FROM actividad_historial")
            history = cursor.fetchone()[0]
            for name in ("precision_calendario", "hora_inicio_planificada", "hora_fin_planificada", "inicio_real_en", "fin_real_en"):
                cursor.execute(f"ALTER TABLE actividad DROP COLUMN {name}")
            cursor.execute("ALTER TABLE dependencia_actividad DROP COLUMN tipo")
        migration = import_module("apps.projects.migrations.0019_task_scheduling_dependencies")
        state = MigrationLoader(None).project_state([("proyectos", "0018_tema_remove_projecttaskstatus_color_and_more")])
        with patch.dict(os.environ, {"TASK_SCHEDULING_MIGRATION_MAINTENANCE": "1"}):
            MigrationExecutor(connection).apply_migration(state, migration.Migration("0019_task_scheduling_dependencies", "proyectos"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT id,fecha_inicio,fecha_fin,estado_codigo,calendario_revision FROM actividad ORDER BY id")
            if old != cursor.fetchall():
                raise RuntimeError("Upgrade changed historical calendar or state")
            cursor.execute("SELECT COUNT(*) FROM actividad_historial")
            if history != cursor.fetchone()[0]:
                raise RuntimeError("Upgrade invented execution history")
        followup = import_module("apps.projects.migrations.0021_record_dependencies_without_retroactive_validation")
        state = MigrationLoader(None).project_state([("proyectos", "0019_task_scheduling_dependencies")])
        with patch.dict(os.environ, {"TASK_SCHEDULING_MIGRATION_MAINTENANCE": "1"}):
            MigrationExecutor(connection).apply_migration(state, followup.Migration("0021_record_dependencies_without_retroactive_validation", "proyectos"))
        cls.upgrade_verified = Task.objects.filter(calendar_precision="day", actual_started_at=None, actual_finished_at=None).count() == 2
        cls.legacy_verified = TaskDependency.objects.filter(dependency_type=None).count() == 1
        Fixtures.tearDown(fixture)

    setUp = Fixtures.setUp

    def tearDown(self):
        TaskCalendarIntent.objects.all().delete()
        Fixtures.tearDown(self)

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            for name in ("gm_au_programacion_historial", "gm_bu_dependencia_tipo", "gm_bi_dependencia_tipo", "gm_bu_programacion", "gm_bi_programacion", "gm_bu_actividad_calendario"):
                cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            for name in ("gm_validar_dependencias_programacion", "gm_validar_limites_dependencia"):
                cursor.execute(f"DROP PROCEDURE IF EXISTS {name}")
        Fixtures.tearDownClass.__func__(cls)

    def test_legacy_stays_informative_and_conversion_preserves_type_without_retroactive_proof(self):
        self.assertTrue(self.upgrade_verified)
        self.assertTrue(self.legacy_verified)
        project=self.project()
        a,b=[self.task(project,schedule=self.hour()) for _ in range(2)]
        self.assertEqual(self.state(b,"EN_CURSO").status_code,200)
        self.assertEqual(self.edge(a,b,"SS").status_code,201)
        self.assertEqual(TaskDependency.objects.count(),1)

    def race(self, project, actions):
        held,attempted,release=Event(),Event(),Event()
        timeline=[];ids={}
        token=issue_access(self.admin,timezone.now()+timedelta(minutes=5))
        def run(index):
            db=connections["default"];db.close();db.ensure_connection()
            with db.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                ids[index]=cursor.fetchone()[0]
            intercepted=False
            def intercept(execute,sql,params,many,context):
                nonlocal intercepted
                target=not intercepted and "FOR UPDATE" in sql.upper() and "FROM `proyecto`" in sql
                if target:
                    intercepted=True;timeline.append({"request":index,"attempt":time.monotonic()})
                    if index==1:attempted.set()
                result=execute(sql,params,many,context)
                if target and index==0:
                    held.set()
                    if not release.wait(15):raise RuntimeError("QA lock not released")
                return result
            try:
                with db.execute_wrapper(intercept):
                    method,path,data=actions[index]
                    result=getattr(Client(),method)(path,data=json.dumps(data),content_type="application/json",HTTP_AUTHORIZATION="Bearer "+token)
                timeline.append({"request":index,"response":time.monotonic(),"status":result.status_code})
                return result
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(run,0)
            self.assertTrue(held.wait(10))
            second=pool.submit(run,1)
            try:self.assertTrue(attempted.wait(10))
            finally:release.set()
            responses=[first.result(20),second.result(20)]
        self.assertNotEqual(ids[0],ids[1])
        self.assertLess(next(x['attempt'] for x in timeline if x.get('request')==1 and 'attempt' in x),next(x['response'] for x in timeline if x.get('request')==0 and 'response' in x))
        target=os.environ.get("GM_SCHEDULING_EVIDENCE")
        if target:
            from pathlib import Path
            with Path(target).open("a",encoding="utf8") as file:file.write(json.dumps({"connections":ids,"timeline":timeline})+"\n")
        return responses

    def test_overlapping_opposite_edges_and_duplicate_edges_are_serialized(self):
        for opposite in [True,False]:
            project=self.project();a,b=[self.task(project,schedule=self.hour()) for _ in range(2)]
            source=b if opposite else a;target=a if opposite else b
            results=self.race(project,[
                ("post",f"/api/projects/{project.pk}/tasks/{b.pk}/dependencies/",{"predecessor_id":a.pk,"dependency_type":"SS"}),
                ("post",f"/api/projects/{project.pk}/tasks/{target.pk}/dependencies/",{"predecessor_id":source.pk,"dependency_type":"SS"}),
            ])
            self.assertEqual([r.status_code for r in results],[201,400])
            self.assertEqual(TaskDependency.objects.filter(project=project).count(),1)

    def test_edge_and_calendar_change_cannot_commit_incompatible_concurrent_proposals(self):
        from uuid import uuid4
        project=self.project();a=self.task(project,schedule=self.hour(8,9));b=self.task(project,schedule=self.hour(10,12))
        results=self.race(project,[
            ("post",f"/api/projects/{project.pk}/tasks/{b.pk}/dependencies/",{"predecessor_id":a.pk,"dependency_type":"FS"}),
            ("patch",f"/api/projects/{project.pk}/tasks/{a.pk}/",{"schedule":self.hour(8,11),"expected_calendar_revision":"1","calendar_intent_id":str(uuid4())}),
        ])
        self.assertEqual([r.status_code for r in results],[201,400])
        a.refresh_from_db();self.assertEqual(a.due_time.hour,9)
        self.assertFalse(TaskCalendarIntent.objects.exists())

    def test_sql_state_procedures_and_raw_link_inserts_cannot_bypass_constraints(self):
        from django.db import DatabaseError, transaction
        from apps.projects.services.audit import audit_actor
        project=self.project();a=self.task(project,schedule=self.hour(8,9));b=self.task(project,schedule=self.hour(10,12))
        self.assertEqual(self.edge(a,b,"FS").status_code,201)
        count=TaskActivity.objects.count()
        for procedure in ["sp_cambiar_estado_actividad", "sp_aplicar_estado_actividad"]:
            with self.assertRaises(DatabaseError),audit_actor(self.admin),transaction.atomic(),connection.cursor() as cursor:
                cursor.callproc(procedure,[project.pk,b.pk,"COMPLETADA",""])
                while cursor.nextset():pass
        b.refresh_from_db()
        self.assertEqual(b.state_code,"PENDIENTE")
        self.assertEqual(TaskActivity.objects.count(),count)
        c=self.task(project,schedule=self.hour(13,14))
        with self.assertRaises(DatabaseError),transaction.atomic():
            TaskDependency.objects.create(project=project,predecessor=a,successor=c)

    def test_migration_maintenance_and_reversal_refuse_loss_of_used_schedule(self):
        migration=import_module("apps.projects.migrations.0019_task_scheduling_dependencies")
        task=self.task(self.project(),schedule=self.hour(8,9))
        with connection.schema_editor() as editor:
            with patch.dict(os.environ,{"TASK_SCHEDULING_MIGRATION_MAINTENANCE":"0"}),self.assertRaises(RuntimeError):
                migration.uninstall(None,editor)
            with patch.dict(os.environ,{"TASK_SCHEDULING_MIGRATION_MAINTENANCE":"1"}),self.assertRaises(RuntimeError):
                migration.uninstall(None,editor)
        task.refresh_from_db();self.assertEqual(task.start_time.hour,8)

    def test_current_migration_reversal_preserves_subtask_and_incompatible_historical_links(self):
        migration = import_module("apps.projects.migrations.0021_record_dependencies_without_retroactive_validation")
        project = self.project()
        a, b = self.task(project), self.task(project)
        self.assertEqual(self.edge(a, b, "FS").status_code, 201)
        with patch.dict(os.environ, {"TASK_SCHEDULING_MIGRATION_MAINTENANCE": "1"}), connection.schema_editor() as editor:
            with self.assertRaises(RuntimeError):
                migration.uninstall(None, editor)
        self.assertTrue(TaskDependency.objects.filter(predecessor=a, successor=b, dependency_type="FS").exists())
        child = self.task(project, parent_id=b.pk)
        self.assertEqual(self.edge(a, child, "SS").status_code, 201)
        with patch.dict(os.environ, {"TASK_SCHEDULING_MIGRATION_MAINTENANCE": "1"}), connection.schema_editor() as editor:
            with self.assertRaises(RuntimeError):
                migration.uninstall(None, editor)
        self.assertTrue(TaskDependency.objects.filter(predecessor=a, successor=child).exists())

    def test_current_migration_roundtrip_keeps_compatible_links_and_calendar(self):
        migration = import_module("apps.projects.migrations.0021_record_dependencies_without_retroactive_validation")
        project = self.project()
        a, b = self.task(project), self.task(project)
        self.assertEqual(self.edge(a, b, "SS").status_code, 201)
        before = list(Task.objects.filter(project=project).values("id", "start_date", "due_date", "state_code", "calendar_revision", "actual_started_at", "actual_finished_at"))
        history = TaskActivity.objects.count()
        with patch.dict(os.environ, {"TASK_SCHEDULING_MIGRATION_MAINTENANCE": "1"}), connection.schema_editor() as editor:
            migration.uninstall(None, editor)
            migration.install(None, editor)
        self.assertEqual(list(Task.objects.filter(project=project).values("id", "start_date", "due_date", "state_code", "calendar_revision", "actual_started_at", "actual_finished_at")), before)
        self.assertEqual(TaskActivity.objects.count(), history)
        self.assertTrue(TaskDependency.objects.filter(predecessor=a, successor=b, dependency_type="SS").exists())
