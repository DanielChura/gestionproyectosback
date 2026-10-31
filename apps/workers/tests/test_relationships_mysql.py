import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from django.db import DatabaseError, connection, connections
from django.test import Client
from django.utils import timezone

from apps.authentication.tokens import issue_access
from apps.organization.models import Specialty
from apps.projects.tests import test_performance as performance_fixtures
from apps.projects.tests import test_projects as project_fixtures
from apps.teams.models import Team, TeamMember
from apps.workers.models import WorkerSpecialty


REPRODUCE = os.environ.get("GM_G08_REPRODUCE") == "1"


class RelationshipFixture(unittest.TestCase):
    tables = [*performance_fixtures.PerformanceRegressionTests.tables, WorkerSpecialty]

    @classmethod
    def setUpClass(cls):
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_relationships":
            raise unittest.SkipTest("GM-G08 requiere su configuración MySQL aislada explícita")
        if (
            os.environ.get("GM_G08_TEST") != "1"
            or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings_test_mysql_relationships"
            or connection.vendor != "mysql"
            or connection.settings_dict["HOST"] != "127.0.0.1"
            or connection.settings_dict["NAME"] != "gm_test_relationships"
        ):
            raise RuntimeError("Se requiere MySQL explícito, desechable y aislado para GM-G08")
        if connection.introspection.table_names():
            raise RuntimeError("La base dedicada debe estar vacía; no se eliminarán tablas existentes")
        with connection.schema_editor() as editor:
            for model in cls.tables:
                editor.create_model(model)
        with connection.cursor() as cursor:
            for sql in Path(__file__).with_name("relationships_reference.sql").read_text(encoding="utf-8").split("@@"):
                cursor.execute(sql.strip())

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE auditoria_evento")
            cursor.execute("DROP PROCEDURE sp_registrar_auditoria")
        with connection.schema_editor() as editor:
            for model in reversed(cls.tables):
                editor.delete_model(model)

    def setUp(self):
        self.fixture = project_fixtures.ProjectFlowTests()
        self.fixture.setUp()
        self.team = Team.objects.create(area=self.fixture.area, name="Equipo de concurrencia QA")
        self.specialty = Specialty.objects.create(name="Civil QA")
        self.tokens = {
            role: issue_access(getattr(self.fixture, role), timezone.now() + timedelta(hours=1))
            for role in ("worker", "admin", "outsider")
        }
        self.client = Client()
        self.members_path = f"teams/teams/{self.team.pk}/members/"
        self.specialties_path = "workers/me/specialties/"

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                cursor.execute("TRUNCATE TABLE auditoria_evento")
                for model in reversed(self.tables):
                    cursor.execute("TRUNCATE TABLE " + connection.ops.quote_name(model._meta.db_table))
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")

    def request(self, method, path, data=None, role="worker"):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + self.tokens[role]} if role else {}
        return Client().generic(method.upper(), "/api/" + path, data=json.dumps(data or {}),
                                content_type="application/json", **headers)

    def add(self, *, days=0):
        return self.request("post", self.members_path, {
            "worker_id": self.fixture.worker.worker_id,
            "joined_date": str(timezone.localdate() - timedelta(days=days)),
        }, "admin")

    def evidence(self, record):
        root = Path(os.environ["GM_G08_REPORT_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        (root / (self._testMethodName + ".json")).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def parallel_reads(self, operations, table):
        barrier = Barrier(2, timeout=10)
        events = []

        def run(index):
            db = connections["default"]
            db.ensure_connection()
            with db.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                identifier = cursor.fetchone()[0]
            reached = False

            def synchronize(execute, sql, params, many, context):
                nonlocal reached
                result = execute(sql, params, many, context)
                normalized = sql.replace(chr(96), "").upper()
                if not reached and normalized.startswith("SELECT") and f"FROM {table.upper()}" in normalized and "JOIN" not in normalized:
                    reached = True
                    events.append({"request": index, "connection": identifier, "read_completed": time.monotonic()})
                    barrier.wait()
                return result

            try:
                with db.execute_wrapper(synchronize):
                    response = operations[index]()
                return response
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, index) for index in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(len(events), 2)
        self.assertEqual(len({item["connection"] for item in events}), 2)
        return results, events

    def audit_rows(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT accion,usuario_id,anterior,nuevo FROM auditoria_evento ORDER BY id")
            return [
                {"action": action, "actor": actor,
                 "before": json.loads(before) if before is not None else None,
                 "after": json.loads(after) if after is not None else None}
                for action, actor, before, after in cursor.fetchall()
            ]

    def assert_actor_cleared(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT @gm_usuario_id,@gm_operacion_id")
            self.assertEqual(cursor.fetchone(), (None, None))


    def locked_requests(self, operations, table):
        held, attempted, release = Event(), Event(), Event()
        identities, timeline = {}, []
        waiting = False

        def run(index):
            db = connections["default"]
            db.ensure_connection()
            with db.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                identities[index] = cursor.fetchone()[0]
            intercepted = False

            def synchronize(execute, sql, params, many, context):
                nonlocal intercepted
                normalized = sql.replace(chr(96), "").upper()
                target = not intercepted and "FOR UPDATE" in normalized and f"FROM {table.upper()}" in normalized
                if target:
                    intercepted = True
                    timeline.append({"request": index, "lock_attempt": time.monotonic()})
                    if index == 1:
                        attempted.set()
                result = execute(sql, params, many, context)
                if target:
                    timeline.append({"request": index, "lock_acquired": time.monotonic()})
                    if index == 0:
                        held.set()
                        if not release.wait(15):
                            raise RuntimeError("No se liberó la primera conexión de prueba")
                return result

            try:
                with db.execute_wrapper(synchronize):
                    response = operations[index]()
                with db.cursor() as cursor:
                    cursor.execute("SELECT @gm_usuario_id,@gm_operacion_id")
                    cleared = cursor.fetchone() == (None, None)
                self.assertTrue(cleared)
                timeline.append({"request": index, "response": time.monotonic(),
                                 "status": response.status_code, "actor_context_cleared": cleared})
                return response
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, 0)
            second = None
            try:
                self.assertTrue(held.wait(10), "La primera solicitud no alcanzó el bloqueo")
                second = pool.submit(run, 1)
                self.assertTrue(attempted.wait(10), "La segunda solicitud no intentó el bloqueo")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT r.PROCESSLIST_ID,b.PROCESSLIST_ID "
                            "FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID "
                            "JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID "
                            "WHERE r.PROCESSLIST_ID=%s AND b.PROCESSLIST_ID=%s",
                            [identities[1], identities[0]],
                        )
                        waiting = cursor.fetchone() is not None
                    if waiting:
                        break
                    time.sleep(0.02)
                self.assertTrue(waiting, "No se observó solapamiento/bloqueo real en MySQL")
            finally:
                release.set()
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(len(set(identities.values())), 2)
        self.evidence({"connections": identities, "mysql_lock_wait_observed": waiting,
                       "timeline": timeline, "statuses": [result.status_code for result in results]})
        return results


@unittest.skipUnless(REPRODUCE, "Reproducción previa a la corrección")
class RelationshipRaceReproduction(RelationshipFixture):

    @classmethod
    def setUpClass(cls):
        from contextlib import ExitStack
        from types import ModuleType
        import sys
        from apps.teams.views import TeamViewSet
        from apps.workers.views import WorkerViewSet

        super().setUpClass()
        checkpoint = Path(__file__).resolve().parents[3] / "docs/reviews/evidencias_bloque2_20261028/checkpoint/backend"

        def baseline(name, package, path):
            module = ModuleType(name)
            module.__package__ = package
            source = (checkpoint / path).read_text(encoding="utf-8")
            exec(compile(source, str(checkpoint / path), "exec"), module.__dict__)
            return module

        service = baseline("baseline_specialties", "apps.workers.services", "apps/workers/services/specialties.py")
        serializers = baseline("baseline_team_serializers", "apps.teams", "apps/teams/serializers.py")
        with patch.dict(sys.modules, {"apps.teams.serializers": serializers}):
            views = baseline("baseline_team_views", "apps.teams", "apps/teams/views.py")

        def specialties(view, worker, request, specialty_id=None):
            from apps.workers.serializers import WorkerSpecialtySerializer

            return service.handle_worker_specialties(worker, request, specialty_id, WorkerSpecialtySerializer)

        cls.baseline_patches = ExitStack()
        cls.baseline_patches.enter_context(patch.object(WorkerViewSet, "_specialties_response", specialties))
        cls.baseline_patches.enter_context(patch.object(TeamViewSet, "members", views.TeamViewSet.members))

    @classmethod
    def tearDownClass(cls):
        cls.baseline_patches.close()
        super().tearDownClass()

    def test_reproduce_lost_specialty_increment(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=1)
        action = lambda: self.request("post", self.specialties_path, {"specialty_id": self.specialty.pk})
        responses, overlap = self.parallel_reads([action, action], "trabajador_especialidad")
        self.assertEqual([response.status_code for response in responses], [200, 200])
        level = WorkerSpecialty.objects.get().level
        self.assertEqual(level, 2)
        self.evidence({"connections": overlap, "statuses": [200, 200], "initial": 1,
                       "expected_without_loss": 3, "observed": level})

    def test_reproduce_duplicate_active_members_with_distinct_dates(self):
        responses, overlap = self.parallel_reads([lambda: self.add(days=1), self.add], "equipo_miembro")
        self.assertEqual([response.status_code for response in responses], [201, 201])
        count = TeamMember.objects.filter(team=self.team, left_date__isnull=True).count()
        self.assertEqual(count, 2)
        self.evidence({"connections": overlap, "statuses": [201, 201], "active_members": count,
                       "historical_unique_present": True})

class RelationshipMySQLTests(RelationshipFixture):

    def test_existing_specialty_increments_do_not_lose_updates(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=1)
        operation = lambda: self.request("post", self.specialties_path, {"specialty_id": self.specialty.pk})
        results = self.locked_requests([operation, operation], "trabajador")
        self.assertEqual([item.status_code for item in results], [200, 200])
        self.assertEqual([item.json()["level"] for item in results], [2, 3])
        self.assertEqual(WorkerSpecialty.objects.get().level, 3)

    def test_concurrent_initial_specialty_additions_preserve_existing_create_then_increment_rule(self):
        operation = lambda: self.request("post", self.specialties_path, {"specialty_id": self.specialty.pk})
        results = self.locked_requests([operation, operation], "trabajador")
        self.assertEqual([item.status_code for item in results], [201, 200])
        self.assertEqual(WorkerSpecialty.objects.count(), 1)
        self.assertEqual(WorkerSpecialty.objects.get().level, 2)

    def test_concurrent_increments_at_upper_bound_reject_only_the_out_of_range_operation(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=2)
        operation = lambda: self.request("patch", self.specialties_path + str(self.specialty.pk) + "/")
        results = self.locked_requests([operation, operation], "trabajador")
        self.assertEqual([item.status_code for item in results], [200, 400])
        self.assertEqual(WorkerSpecialty.objects.get().level, 3)

    def test_nested_replacement_and_increment_use_the_same_worker_lock(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=1)
        first = lambda: self.request("patch", f"workers/{self.fixture.worker.worker_id}/", {
            "specialties": [{"id": self.specialty.pk, "level": 2}],
        }, "admin")
        second = lambda: self.request("post", self.specialties_path, {"specialty_id": self.specialty.pk})
        results = self.locked_requests([first, second], "trabajador")
        self.assertEqual([item.status_code for item in results], [200, 200])
        self.assertEqual(WorkerSpecialty.objects.get().level, 3)

    def test_specialty_delete_serializes_with_increment_without_resurrection(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=1)
        payload = {"specialty_id": self.specialty.pk}
        first = lambda: self.request("post", self.specialties_path, payload)
        second = lambda: self.request("delete", self.specialties_path, payload)
        results = self.locked_requests([first, second], "trabajador")
        self.assertEqual([item.status_code for item in results], [200, 204])
        self.assertFalse(WorkerSpecialty.objects.exists())

    def test_distinct_join_dates_cannot_create_two_active_members(self):
        results = self.locked_requests([lambda: self.add(days=1), self.add], "equipo")
        self.assertEqual([item.status_code for item in results], [201, 400])
        self.assertEqual(TeamMember.objects.filter(team=self.team, left_date__isnull=True).count(), 1)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "INSERT")
        self.assertEqual(rows[0]["actor"], self.fixture.admin.pk)

    def test_concurrent_reincorporations_preserve_record_and_audited_previous_period(self):
        created = self.add(days=5)
        identifier = created.json()["id"]
        self.assertEqual(self.request("delete", self.members_path + str(identifier) + "/", role="admin").status_code, 204)
        prior = TeamMember.objects.get(pk=identifier)
        results = self.locked_requests([lambda: self.add(days=1), self.add], "equipo")
        self.assertEqual([item.status_code for item in results], [201, 400])
        self.assertEqual(results[0].json()["id"], identifier)
        self.assertEqual(TeamMember.objects.count(), 1)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["before"]["fecha_ingreso"], str(prior.joined_date))
        self.assertEqual(rows[-1]["before"]["fecha_salida"], str(prior.left_date))
        self.assertIsNone(rows[-1]["after"]["fecha_salida"])
        self.assertEqual({row["actor"] for row in rows}, {self.fixture.admin.pk})

    def test_removal_and_reincorporation_share_the_team_lock_and_audit_each_confirmation_once(self):
        identifier = self.add(days=2).json()["id"]
        first = lambda: self.request("delete", self.members_path + str(identifier) + "/", role="admin")
        results = self.locked_requests([first, self.add], "equipo")
        self.assertEqual([item.status_code for item in results], [204, 201])
        self.assertEqual(results[1].json()["id"], identifier)
        self.assertEqual(TeamMember.objects.count(), 1)
        self.assertIsNone(TeamMember.objects.get().left_date)
        self.assertEqual([row["action"] for row in self.audit_rows()], ["INSERT", "UPDATE", "UPDATE"])

    def test_all_specialty_routes_validate_input_and_keep_ownership(self):
        routes = [
            ("post", self.specialties_path, "worker"),
            ("patch", self.specialties_path + str(self.specialty.pk) + "/", "worker"),
            ("post", f"workers/{self.fixture.worker.worker_id}/specialties/", "admin"),
            ("patch", f"workers/{self.fixture.worker.worker_id}/specialties/{self.specialty.pk}/", "admin"),
        ]
        for method, path, role in routes:
            for payload in ({"level": 0}, {"level": 4}, {"level": "mal"}, {"increment": "mal"}, {"increment": -5}):
                WorkerSpecialty.objects.all().delete()
                WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=1)
                with self.subTest(path=path, payload=payload):
                    result = self.request(method, path, {"specialty_id": self.specialty.pk, **payload}, role)
                    self.assertEqual(result.status_code, 400, result.content)
                    self.assertEqual(WorkerSpecialty.objects.get().level, 1)
        for payload in ({}, {"specialty_id": 0}, {"specialty_id": "x"}, {"specialty_id": 999999}):
            self.assertEqual(self.request("post", self.specialties_path, payload).status_code, 400)
        target = f"workers/{self.fixture.admin.worker_id}/specialties/"
        self.assertEqual(self.request("post", target, {"specialty_id": self.specialty.pk}).status_code, 403)
        self.assertEqual(self.request("post", self.specialties_path, {"specialty_id": self.specialty.pk}, role=None).status_code, 401)
        self.assertFalse(WorkerSpecialty.objects.filter(worker=self.fixture.admin.worker).exists())

    def test_level_assignment_delta_and_initial_defaults_keep_the_existing_semantics(self):
        response = self.request("post", self.specialties_path, {"id": self.specialty.pk, "increment": 2})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["level"], 1)
        path = self.specialties_path + str(self.specialty.pk) + "/"
        response = self.request("patch", path, {"level": 3, "increment": -1, "worker_id": self.fixture.admin.worker_id})
        self.assertEqual(response.json()["level"], 3)
        response = self.request("patch", path, {"increment": -2})
        self.assertEqual(response.json()["level"], 1)
        self.assertEqual(self.request("patch", path, {"increment": 0}).json()["level"], 1)
        self.assertEqual(self.request("patch", path, {"level": None}).json()["level"], 2)
        self.assertFalse(WorkerSpecialty.objects.filter(worker=self.fixture.admin.worker).exists())

    def test_nested_create_update_and_replace_validate_before_writing(self):
        worker_id = self.fixture.worker.worker_id
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=2)
        original_name = self.fixture.worker.worker.first_names
        for specialties in (
            [{"id": self.specialty.pk, "level": 0}],
            [{"id": 999999, "level": 1}],
            [{"id": self.specialty.pk}, {"id": self.specialty.pk}],
        ):
            response = self.request("patch", f"workers/{worker_id}/", {
                "first_names": "No debe persistir", "specialties": specialties,
            }, "admin")
            self.assertEqual(response.status_code, 400, response.content)
            self.fixture.worker.worker.refresh_from_db()
            self.assertEqual(self.fixture.worker.worker.first_names, original_name)
            self.assertEqual(WorkerSpecialty.objects.get().level, 2)
        created = self.request("post", "workers/", {
            "first_names": "Nuevo QA", "last_names": "Aislado", "specialties": [{"id": self.specialty.pk}],
        }, "admin")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(WorkerSpecialty.objects.get(worker_id=created.json()["id"]).level, 1)
        response = self.request("patch", f"workers/{worker_id}/", {"first_names": "Conservar"}, "admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WorkerSpecialty.objects.get(worker_id=worker_id).level, 2)
        self.assertEqual(self.request("patch", f"workers/{worker_id}/", {"specialties": []}, "admin").status_code, 200)
        self.assertFalse(WorkerSpecialty.objects.filter(worker_id=worker_id).exists())

    def test_invalid_membership_inputs_and_worker_permissions_do_not_write_or_audit(self):
        for payload in (
            {}, {"worker_id": 0}, {"worker_id": "mal"}, {"worker_id": 999999},
            {"worker_id": self.fixture.worker.worker_id, "joined_date": "mal"},
            {"worker_id": self.fixture.worker.worker_id, "joined_date": str(timezone.localdate() + timedelta(days=1))},
        ):
            self.assertEqual(self.request("post", self.members_path, payload, "admin").status_code, 400)
        self.fixture.worker.worker.active = False
        self.fixture.worker.worker.save(update_fields=["active"])
        self.assertEqual(self.add().status_code, 400)
        self.fixture.worker.worker.active = True
        self.fixture.worker.worker.save(update_fields=["active"])
        self.team.active = False
        self.team.save(update_fields=["active"])
        self.assertEqual(self.add().status_code, 400)
        self.team.active = True
        self.team.save(update_fields=["active"])
        payload = {"worker_id": self.fixture.worker.worker_id}
        self.assertEqual(self.request("post", self.members_path, payload).status_code, 403)
        self.assertEqual(self.request("post", self.members_path, payload, role=None).status_code, 401)
        self.assertEqual(self.request("get", self.members_path).status_code, 200)
        self.assertFalse(TeamMember.objects.exists())
        self.assertEqual(self.audit_rows(), [])
        self.assert_actor_cleared()

    def test_membership_rollback_restores_member_and_audit_when_save_fails(self):
        identifier = self.add(days=2).json()["id"]
        original_save = TeamMember.save

        def failure(member, *args, **kwargs):
            original_save(member, *args, **kwargs)
            raise RuntimeError("Fallo QA después de escribir")

        with patch.object(TeamMember, "save", failure):
            with self.assertRaisesRegex(RuntimeError, "Fallo QA"):
                self.request("delete", self.members_path + str(identifier) + "/", role="admin")
        self.assertIsNone(TeamMember.objects.get().left_date)
        self.assertEqual(len(self.audit_rows()), 1)
        self.assert_actor_cleared()

    def test_reincorporation_rollback_preserves_closed_record_and_audit(self):
        identifier = self.add(days=2).json()["id"]
        self.request("delete", self.members_path + str(identifier) + "/", role="admin")
        before = TeamMember.objects.get()
        original_save = TeamMember.save

        def failure(member, *args, **kwargs):
            original_save(member, *args, **kwargs)
            raise RuntimeError("Fallo QA después de reincorporar")

        with patch.object(TeamMember, "save", failure):
            with self.assertRaisesRegex(RuntimeError, "Fallo QA"):
                self.add()
        after = TeamMember.objects.get()
        self.assertEqual((after.pk, after.joined_date, after.left_date), (before.pk, before.joined_date, before.left_date))
        self.assertEqual(len(self.audit_rows()), 2)
        self.assert_actor_cleared()

    def test_nested_replacement_rolls_back_metadata_and_specialties(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=2)
        original_name = self.fixture.worker.worker.first_names
        original_bulk = WorkerSpecialty.objects.bulk_create

        def failure(*args, **kwargs):
            original_bulk(*args, **kwargs)
            raise RuntimeError("Fallo QA después de reemplazar")

        with patch("apps.workers.services.specialties.WorkerSpecialty.objects.bulk_create", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Fallo QA"):
                self.request("patch", f"workers/{self.fixture.worker.worker_id}/", {
                    "first_names": "No persistir", "specialties": [{"id": self.specialty.pk, "level": 3}],
                }, "admin")
        self.fixture.worker.worker.refresh_from_db()
        self.assertEqual(self.fixture.worker.worker.first_names, original_name)
        self.assertEqual(WorkerSpecialty.objects.get().level, 2)

    def test_missing_audit_actor_rejects_membership_and_rolls_back(self):
        from contextlib import nullcontext

        with patch("apps.teams.views.audit_actor", return_value=nullcontext()):
            with self.assertRaises(DatabaseError):
                self.add()
        self.assertFalse(TeamMember.objects.exists())
        self.assertEqual(self.audit_rows(), [])
        self.assert_actor_cleared()

    def test_closed_history_cannot_be_deleted_and_existing_conflicts_are_not_merged(self):
        identifier = self.add(days=2).json()["id"]
        self.request("delete", self.members_path + str(identifier) + "/", role="admin")
        with self.assertRaises(DatabaseError):
            TeamMember.objects.filter(pk=identifier).delete()
        self.assertEqual(TeamMember.objects.count(), 1)
        self.assertEqual(len(self.audit_rows()), 2)
        from apps.projects.services.audit import audit_actor

        with audit_actor(self.fixture.admin):
            TeamMember.objects.create(team=self.team, worker=self.fixture.worker.worker, joined_date=timezone.localdate())
            TeamMember.objects.create(team=self.team, worker=self.fixture.worker.worker, joined_date=timezone.localdate() - timedelta(days=1))
        before = list(TeamMember.objects.values("id", "joined_date", "left_date"))
        self.assertEqual(self.add(days=1).status_code, 400)
        self.assertEqual(list(TeamMember.objects.values("id", "joined_date", "left_date")), before)
        self.assertEqual(len(self.audit_rows()), 4)



    def test_project_team_snapshot_and_removal_lock_team_before_members(self):
        identifier = self.add(days=1).json()["id"]
        first = lambda: self.request("post", "projects/", self.fixture.project_payload(
            mode="assigned", participant_source="teams", team_ids=[self.team.pk],
        ), "admin")
        second = lambda: self.request("delete", self.members_path + str(identifier) + "/", role="admin")
        results = self.locked_requests([first, second], "equipo")
        self.assertEqual([item.status_code for item in results], [201, 204])
        from apps.projects.models import ProjectMember

        self.assertTrue(ProjectMember.objects.filter(
            project_id=results[0].json()["id"], worker_id=self.fixture.worker.worker_id,
        ).exists())
        self.assertIsNotNone(TeamMember.objects.get().left_date)


    def test_reincorporation_with_existing_historical_join_date_returns_validation_error(self):
        identifier = self.add(days=5).json()["id"]
        self.request("delete", self.members_path + str(identifier) + "/", role="admin")
        from apps.projects.services.audit import audit_actor

        with audit_actor(self.fixture.admin):
            TeamMember.objects.create(
                team=self.team, worker=self.fixture.worker.worker,
                joined_date=timezone.localdate() - timedelta(days=2),
                left_date=timezone.localdate() - timedelta(days=1),
            )
        before = list(TeamMember.objects.order_by("id").values("id", "joined_date", "left_date"))
        result = self.add(days=5)
        self.assertEqual(result.status_code, 400, result.content)
        self.assertEqual(list(TeamMember.objects.order_by("id").values("id", "joined_date", "left_date")), before)
        self.assertEqual(len(self.audit_rows()), 3)
        self.assert_actor_cleared()

    def test_specialty_upsert_rolls_back_when_save_fails(self):
        WorkerSpecialty.objects.create(worker=self.fixture.worker.worker, specialty=self.specialty, level=1)
        original_save = WorkerSpecialty.save

        def failure(relation, *args, **kwargs):
            original_save(relation, *args, **kwargs)
            raise RuntimeError("Fallo QA después del incremento")

        with patch.object(WorkerSpecialty, "save", failure):
            with self.assertRaisesRegex(RuntimeError, "Fallo QA"):
                self.request("post", self.specialties_path, {"specialty_id": self.specialty.pk})
        self.assertEqual(WorkerSpecialty.objects.get().level, 1)


def load_tests(loader, tests, pattern):
    target = RelationshipRaceReproduction if REPRODUCE else RelationshipMySQLTests
    return loader.loadTestsFromTestCase(target)
