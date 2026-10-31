import json
import unittest
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.authentication.models import RecoveryRequest, User
from apps.projects.models import Project
from . import test_performance as fixtures
from .pagination_seed import seed_lists


class ListPaginationTests(unittest.TestCase):
    tables = fixtures.PerformanceRegressionTests.tables
    setUpClass = classmethod(fixtures.PerformanceRegressionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.PerformanceRegressionTests.tearDownClass.__func__)
    tearDown = fixtures.PerformanceRegressionTests.tearDown

    def setUp(self):
        fixtures.PerformanceRegressionTests.setUp(self)
        self.context = seed_lists(self.fixture)

    def get(self, path, query=None, role="admin"):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + self.tokens[role]} if role else {}
        return self.client.get("/api/" + path, query or {}, **headers)

    def pages(self, path, query=None, role="admin"):
        query = dict(query or {})
        rows, cursors = [], []
        while True:
            response = self.get(path, query, role)
            self.assertEqual(response.status_code, 200, response.content)
            page = response.json()
            self.assertLessEqual(len(page["results"]), query.get("page_size", 25))
            rows.extend(page["results"])
            cursors.append(page["cursor"])
            if not page["next_cursor"]:
                break
            query["cursor"] = page["next_cursor"]
            self.assertLess(len(cursors), 30)
        return rows, page, cursors

    def post(self, path, payload, role="admin"):
        return self.client.post("/api/" + path, data=json.dumps(payload), content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + self.tokens[role])

    def test_projects_reach_every_authorized_record_after_200(self):
        for role, count in (("admin", 240), ("worker", 235)):
            rows, final, _ = self.pages("projects/", role=role)
            identifiers = [row["id"] for row in rows]
            self.assertEqual(len(identifiers), count)
            self.assertEqual(len(set(identifiers)), count)
            self.assertEqual(identifiers, sorted(identifiers, reverse=True))
            self.assertIn(self.context["first_project"], identifiers)
            self.assertEqual(final["total"], count)
        rows, final, _ = self.pages("projects/", role="outsider")
        self.assertEqual(rows, [])
        self.assertEqual(final["total"], 0)

    def test_accounts_reach_every_record_after_50_and_are_admin_only(self):
        rows, final, _ = self.pages("auth/admin/accounts/")
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), 79)
        self.assertEqual(len(set(ids)), 79)
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertIn(self.context["first_account"], ids)
        self.assertEqual(final["total"], 79)
        for role in ("worker", "outsider", None):
            self.assertEqual(self.get("auth/admin/accounts/", role=role).status_code, 403 if role else 401)

    def test_recoveries_reach_after_50_with_tied_timestamps_and_only_pending(self):
        rows, final, _ = self.pages("auth/admin/recovery-requests/")
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), 70)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 70)
        self.assertIn(self.context["last_recovery"], ids)
        self.assertNotIn(self.context["resolved_recovery"], ids)
        self.assertEqual(final["total"], 70)
        self.assertEqual(self.get("auth/admin/recovery-requests/", role="worker").status_code, 403)

    def test_search_filters_apply_before_paging(self):
        expected = [
            ("projects/", {"search": "Proyecto QA 000"}, self.context["first_project"]),
            ("auth/admin/accounts/", {"search": "QA000"}, self.context["first_account"]),
            ("auth/admin/recovery-requests/", {"search": "QA075"}, self.context["last_recovery"]),
        ]
        for path, query, identifier in expected:
            page = self.get(path, query).json()
            self.assertEqual([row["id"] for row in page["results"]], [identifier])
            self.assertEqual(page["total"], 1)
        rows, _, _ = self.pages("auth/admin/accounts/", {"active": "false"})
        self.assertTrue(rows)
        self.assertTrue(all(not row["active"] for row in rows))
        page = self.get("projects/", {"area_id": self.context["area"], "associated": "true"}).json()
        self.assertEqual(page["total"], 235)
        self.assertTrue(all(row["has_associated_workers"] for row in page["results"]))
        self.assertEqual(self.get("projects/", {"state_code": "NO_EXISTE"}).json()["total"], 0)

    def test_cursor_rejects_tampering_filter_changes_and_cross_principals(self):
        cursor = self.get("projects/").json()["next_cursor"]
        self.assertEqual(self.get("projects/", {"cursor": cursor + "x"}).status_code, 400)
        self.assertEqual(self.get("projects/", {"cursor": cursor, "search": "otro"}).status_code, 400)
        self.assertEqual(self.get("projects/", {"cursor": cursor}, "worker").status_code, 400)
        self.assertEqual(self.get("auth/admin/accounts/", {"cursor": cursor}).status_code, 400)
        self.assertEqual(self.get("projects/", {"cursor": cursor, "page_size": 50}).status_code, 400)

    def test_bounds_empty_pages_and_empty_filters_are_explicit(self):
        for path in ("projects/", "auth/admin/accounts/", "auth/admin/recovery-requests/"):
            for value in (0, 51, "mal"):
                response = self.get(path, {"page_size": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("page_size", response.json())
            self.assertLessEqual(len(self.get(path, {"page_size": 50}).json()["results"]), 50)
            empty = self.get(path, {"search": "ningun-registro-qa"}).json()
            self.assertEqual((empty["results"], empty["total"], empty["next_cursor"]), ([], 0, None))

    def test_insertions_and_removal_do_not_shift_the_traversal(self):
        first = self.get("projects/").json()
        initial = set(Project.objects.values_list("pk", flat=True))
        newer = Project.objects.create(code="NEW-PAGE", name="Proyecto nuevo QA", area=self.fixture.area,
                                       type_code="GENERAL", start_date=self.fixture.today, available=True)
        Project.objects.filter(pk=first["results"][0]["id"]).update(archived=True)
        rows, final, _ = self.pages("projects/", {"cursor": first["next_cursor"]})
        identifiers = [row["id"] for row in first["results"] + rows]
        self.assertEqual(set(identifiers), initial)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertNotIn(newer.pk, identifiers)
        self.assertEqual(final["total"], 239)
        fresh = self.get("projects/").json()
        self.assertEqual(fresh["results"][0]["id"], newer.pk)
        previous = self.get("projects/", {"cursor": first["cursor"]}).json()
        self.assertNotIn(newer.pk, [row["id"] for row in previous["results"]])

    def test_empty_last_page_after_resolution_remains_valid(self):
        first = self.get("auth/admin/recovery-requests/").json()
        RecoveryRequest.objects.exclude(pk__in=[row["id"] for row in first["results"]]).update(resolved_at=timezone.now())
        page = self.get("auth/admin/recovery-requests/", {"cursor": first["next_cursor"]}).json()
        self.assertEqual(page["results"], [])
        self.assertEqual(page["total"], 25)
        self.assertIsNone(page["next_cursor"])

    def test_mutations_refresh_the_correct_filtered_collection(self):
        with patch("apps.authentication.views.send_link", return_value="qa-message"):
            response = self.post("auth/admin/recovery-requests/" + str(self.context["last_recovery"]) + "/issue/", {})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.get("auth/admin/recovery-requests/", {"search": "QA075"}).json()["results"], [])
        payload = self.fixture.project_payload(name="Alta paginada QA")
        response = self.post("projects/", payload)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.get("projects/", {"search": "Alta paginada QA"}).json()["results"][0]["id"], response.json()["id"])
        User.objects.filter(pk=self.context["last_account"]).update(is_active=False)
        page = self.get("auth/admin/accounts/", {"search": "QA075", "active": "false"}).json()
        self.assertEqual(page["total"], 1)

    def test_page_queries_and_payloads_are_bounded(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.get("projects/")
        self.assertEqual(len(response.json()["results"]), 25)
        self.assertEqual(response.json()["total"], 240)
        self.assertLess(len(queries), 25)
        self.assertTrue(any("LIMIT 26" in item["sql"] for item in queries.captured_queries))
        self.assertLess(len(response.content), 40000)
