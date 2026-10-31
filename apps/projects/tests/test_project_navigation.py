import unittest

from django.test.utils import CaptureQueriesContext
from django.db import connection

from apps.organization.models import Area
from apps.projects.models import Project, ProjectMember
from apps.workers.models import WorkerArea
from . import test_performance as fixtures


class ProjectNavigationTests(unittest.TestCase):
    tables = fixtures.PerformanceRegressionTests.tables
    setUpClass = classmethod(fixtures.PerformanceRegressionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.PerformanceRegressionTests.tearDownClass.__func__)
    tearDown = fixtures.PerformanceRegressionTests.tearDown

    def setUp(self):
        fixtures.PerformanceRegressionTests.setUp(self)
        f = self.fixture
        self.old_area = Area.objects.create(code="NAV_OLD", name="Área con proyecto antiguo", active=True)
        WorkerArea.objects.create(worker=f.worker.worker, area=f.area)
        WorkerArea.objects.create(worker=f.worker.worker, area=self.old_area)
        self.old_project = Project.objects.create(code="NAV_OLD", name="Proyecto antiguo QA", area=self.old_area,
            type_code="GENERAL", start_date=f.today, available=False)
        ProjectMember.objects.create(project=self.old_project, worker=f.worker.worker)
        Project.objects.bulk_create([Project(code=f"NAV_{i}", name=f"Proyecto QA {i:03}", area=f.area,
            type_code="GENERAL", start_date=f.today, available=True) for i in range(235)])
        self.main = list(Project.objects.filter(area=f.area).order_by("pk"))
        ProjectMember.objects.bulk_create([ProjectMember(project=p, worker=f.worker.worker) for p in self.main[:-2]])
        self.other_ids = []
        for i in range(30):
            area = Area.objects.create(code=f"NAV_AREA_{i}", name=f"Área adicional QA {i:02}", active=True)
            WorkerArea.objects.create(worker=f.outsider.worker, area=area)
            project = Project.objects.create(code=f"NAV_AREA_PROJECT_{i}", name=f"Proyecto adicional QA {i:02}",
                area=area, type_code="GENERAL", start_date=f.today, available=False)
            ProjectMember.objects.create(project=project, worker=f.outsider.worker)
            self.other_ids.append(area.pk)

    def get(self, path="projects/areas/", query=None, role="admin"):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + self.tokens[role]} if role else {}
        return self.client.get("/api/" + path, query or {}, **headers)

    def all_rows(self, path, query=None, role="admin"):
        rows, query = [], dict(query or {})
        for _ in range(20):
            reply = self.get(path, query, role)
            self.assertEqual(reply.status_code, 200, reply.content)
            page = reply.json()
            self.assertLessEqual(len(page["results"]), 25)
            rows.extend(page["results"])
            if page["next_cursor"] is None:
                return rows
            query["cursor"] = page["next_cursor"]
        self.fail("La continuación no termina")

    def test_area_catalog_reaches_areas_missing_from_the_global_first_page(self):
        first = self.get("projects/", {"associated": True}).json()["results"]
        self.assertNotIn(self.old_area.pk, {row["areas"][0]["id"] for row in first})
        rows = self.all_rows("projects/areas/", {"associated": True})
        ids = [row["id"] for row in rows]
        self.assertEqual(ids, sorted(set(ids), reverse=True))
        self.assertEqual(len(ids), 32)
        self.assertIn(self.old_area.pk, ids)
        self.assertTrue(all(set(row) == {"id", "name"} for row in rows))

    def test_area_project_pages_reach_beyond_200_and_keep_their_scope(self):
        rows = self.all_rows("projects/", {"area_id": self.fixture.area.pk, "associated": True})
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), 233)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(self.main[0].pk, ids)
        self.assertEqual(ids, sorted(ids, reverse=True))
        all_projects = self.all_rows("projects/", {"area_id": self.fixture.area.pk})
        self.assertEqual(len(all_projects), 235)
        self.assertNotIn(self.main[-1].pk, ids)

    def test_worker_areas_are_authorized_and_do_not_expose_admin_catalog(self):
        for associated in (True, False):
            rows = self.all_rows("projects/areas/", {"associated": associated}, "worker")
            expected = {self.fixture.area.pk, self.old_area.pk} if associated else {self.fixture.area.pk}
            self.assertEqual({row["id"] for row in rows}, expected)
        self.assertEqual(self.get(query={"area_id": self.other_ids[0]}, role="worker").json()["results"], [])
        self.assertEqual(self.get("organization/areas/", role="worker").status_code, 403)
        self.assertEqual(self.get(role=None).status_code, 401)
        reply = self.client.post("/api/projects/areas/", {}, HTTP_AUTHORIZATION="Bearer " + self.tokens["admin"])
        self.assertEqual(reply.status_code, 405)

    def test_direct_project_filter_is_bounded_authorized_and_minimal(self):
        page = self.get("projects/", {"project_id": self.old_project.pk, "associated": True}, "worker").json()
        self.assertEqual([row["id"] for row in page["results"]], [self.old_project.pk])
        self.assertIsNone(page["next_cursor"])
        self.assertNotIn("task_states", page["results"][0])
        self.assertNotIn("participants", page["results"][0])
        self.assertEqual(self.get("projects/", {"project_id": self.main[-1].pk, "associated": True}).json()["results"], [])
        self.assertEqual(self.get("projects/", {"project_id": self.old_project.pk}, "outsider").json()["results"], [])
        for value in (0, -1, "bad"):
            self.assertEqual(self.get("projects/", {"project_id": value}).status_code, 400)

    def test_area_cursor_is_bound_to_scope_actor_filters_and_resource(self):
        cursor = self.get(query={"associated": True}).json()["next_cursor"]
        for path, query, role in (
            ("projects/areas/", {"cursor": cursor, "associated": False}, "admin"),
            ("projects/areas/", {"cursor": cursor, "associated": True}, "worker"),
            ("projects/", {"cursor": cursor, "associated": True}, "admin"),
            ("projects/areas/", {"cursor": cursor + "x", "associated": True}, "admin"),
        ):
            self.assertEqual(self.get(path, query, role).status_code, 400)
        for size in (0, 51):
            self.assertEqual(self.get(query={"page_size": size}).status_code, 400)

    def test_creation_rename_move_and_archive_are_visible_only_in_affected_queries(self):
        project = self.old_project
        Project.objects.filter(pk=project.pk).update(name="Nombre actualizado QA", area=self.fixture.area)
        self.assertEqual(self.get(query={"area_id": self.old_area.pk}).json()["results"], [])
        row = self.get("projects/", {"project_id": project.pk}).json()["results"][0]
        self.assertEqual(row["name"], "Nombre actualizado QA")
        self.assertEqual(row["areas"][0]["id"], self.fixture.area.pk)
        Project.objects.filter(pk=project.pk).update(archived=True)
        self.assertEqual(self.get("projects/", {"project_id": project.pk}).json()["results"], [])
        fresh = Project.objects.create(code="NAV_FRESH", name="Alta QA", area=self.old_area,
            type_code="GENERAL", start_date=self.fixture.today, available=True)
        self.assertEqual(self.get(query={"area_id": self.old_area.pk}).json()["results"], [{"id": self.old_area.pk, "name": self.old_area.name}])
        self.assertEqual(self.get("projects/", {"area_id": self.old_area.pk}).json()["results"][0]["id"], fresh.pk)

    def test_area_reads_do_not_fetch_tasks_or_details_and_empty_is_valid(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.get(query={"associated": True})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any('FROM "actividad"' in item["sql"] for item in queries))
        self.assertEqual(self.get(query={"search": "no-match"}).json()["results"], [])
        self.assertIsNone(self.get(query={"search": "no-match"}).json()["next_cursor"])
