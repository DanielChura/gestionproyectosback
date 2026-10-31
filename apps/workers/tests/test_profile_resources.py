import json
import unittest

from django.db import connection
from apps.organization.models import Specialty
from apps.workers.models import Worker, WorkerSpecialty
from apps.teams.models import Team, TeamMember
from apps.projects.tests.test_performance import PerformanceRegressionTests as Fixtures


class ProfileResourceTests(unittest.TestCase):
    tables = [*Fixtures.tables, WorkerSpecialty]
    setUpClass = classmethod(Fixtures.setUpClass.__func__)
    tearDownClass = classmethod(Fixtures.tearDownClass.__func__)
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown

    def request(self, method, path, data=None, role="worker"):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + self.tokens[role]} if role else {}
        return self.client.generic(method.upper(), "/api/" + path, data=json.dumps(data or {}),
                                   content_type="application/json", **headers)

    def test_options_require_authentication_and_expose_only_identifier_and_name(self):
        first = Specialty.objects.create(name="BIM QA")
        second = Specialty.objects.create(name="Civil QA")
        for role in ("worker", "admin"):
            response = self.request("get", "workers/me/specialty-options/", role=role)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), [{"id": first.pk, "name": first.name}, {"id": second.pk, "name": second.name}])
        self.assertEqual(self.request("get", "workers/me/specialty-options/", role=None).status_code, 401)

    def test_options_are_read_only_for_both_roles(self):
        for role in ("worker", "admin"):
            for method in ("post", "put", "patch", "delete"):
                response = self.request(method, "workers/me/specialty-options/", {"name": "No crear"}, role)
                self.assertEqual(response.status_code, 405)
        self.assertEqual(Specialty.objects.count(), 0)

    def test_administrative_crud_stays_forbidden_to_worker(self):
        specialty = Specialty.objects.create(name="Civil QA")
        for method, path in (("get", "organization/specialties/"), ("post", "organization/specialties/"),
                             ("get", f"organization/specialties/{specialty.pk}/"),
                             ("patch", f"organization/specialties/{specialty.pk}/"),
                             ("delete", f"organization/specialties/{specialty.pk}/")):
            self.assertEqual(self.request(method, path, {"name": "No modificar"}).status_code, 403)
        self.assertEqual(self.request("get", "organization/specialties/", role="admin").status_code, 200)
        specialty.refresh_from_db()
        self.assertEqual(specialty.name, "Civil QA")

    def test_own_specialties_keep_ownership_and_existing_add_remove_contract(self):
        specialty = Specialty.objects.create(name="BIM QA")
        foreign = WorkerSpecialty.objects.create(worker=self.fixture.admin.worker, specialty=specialty, level=3)
        result = self.request("post", "workers/me/specialties/", {"specialty_id": specialty.pk,
                              "worker_id": self.fixture.admin.worker_id})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(self.request("get", "workers/me/specialties/").json(), [{"id": specialty.pk, "name": specialty.name, "level": 1}])
        self.assertEqual(self.request("delete", "workers/me/specialties/", {"specialty_id": specialty.pk}).status_code, 204)
        self.assertFalse(WorkerSpecialty.objects.filter(worker=self.fixture.worker.worker).exists())
        foreign.refresh_from_db()
        self.assertEqual(foreign.level, 3)

    def test_other_worker_actions_and_team_writes_remain_forbidden(self):
        worker = self.fixture.admin.worker
        for method, path, data in (("patch", f"workers/{worker.pk}/", {"first_names": "No modificar"}),
                                   ("post", f"workers/{worker.pk}/specialties/", {}),
                                   ("post", "teams/teams/", {"name": "No crear"})):
            self.assertEqual(self.request(method, path, data).status_code, 403)
        self.assertEqual(self.request("get", "workers/").status_code, 403)

    def test_worker_area_and_active_filters_are_honored_without_touching_other_records(self):
        inactive = Worker.objects.create(area=self.fixture.other_area, code="QA-INACTIVE", first_names="Filtro",
                                         last_names="Inactivo", active=False)
        path = f"workers/?area_id={self.fixture.other_area.pk}&active=false"
        response = self.request("get", path, role="admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()], [inactive.pk])
        response = self.request("get", f"workers/?area_id={self.fixture.area.pk}&active=true", role="admin")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(inactive.pk, [item["id"] for item in response.json()])

    def test_revoked_account_cannot_read_options(self):
        self.fixture.worker.worker.active = False
        self.fixture.worker.worker.save(update_fields=["active"])
        self.assertEqual(self.request("get", "workers/me/specialty-options/").status_code, 401)

    def test_fixture_is_explicitly_isolated(self):
        self.assertEqual(connection.vendor, "sqlite")
        self.assertEqual(connection.settings_dict["NAME"], ":memory:")

    def test_team_members_are_readable_but_writable_only_by_administrator(self):
        team = Team.objects.create(area=self.fixture.area, name="Permisos QA")
        path = f"teams/teams/{team.pk}/members/"
        payload = {"worker_id": self.fixture.worker.worker_id}
        self.assertEqual(self.request("get", path).status_code, 200)
        self.assertEqual(self.request("post", path, payload).status_code, 403)
        self.assertFalse(TeamMember.objects.filter(team=team).exists())
        added = self.request("post", path, payload, role="admin")
        self.assertEqual(added.status_code, 201)
        remove = f"teams/teams/{team.pk}/members/{added.json()['id']}/"
        self.assertEqual(self.request("delete", remove).status_code, 403)
        self.assertEqual(self.request("delete", remove, role="admin").status_code, 204)
        self.assertEqual(self.request("get", path).json(), [])
        self.assertEqual(len(self.request("get", path + "?all=1").json()), 1)
