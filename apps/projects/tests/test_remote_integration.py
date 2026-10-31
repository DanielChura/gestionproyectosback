import json
import unittest
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.authentication.models import AuthToken
from apps.authentication.tokens import InvalidToken, consume_stored, digest_token, issue_stored
from apps.projects.models import TaskOrder
from apps.publications.models import ProjectPublication, PublicationAssignment, PublicationRequirement
from . import test_performance as performance


class RemoteIntegrationTests(unittest.TestCase):
    tables = performance.PerformanceRegressionTests.tables + [ProjectPublication, PublicationRequirement, PublicationAssignment]
    setUpClass = classmethod(performance.PerformanceRegressionTests.setUpClass.__func__)
    tearDownClass = classmethod(performance.PerformanceRegressionTests.tearDownClass.__func__)
    setUp = performance.PerformanceRegressionTests.setUp
    tearDown = performance.PerformanceRegressionTests.tearDown
    project = performance.PerformanceRegressionTests.project
    task = performance.PerformanceRegressionTests.task
    get = performance.PerformanceRegressionTests.get

    def send(self, method, path, data, role="admin"):
        return getattr(self.client, method)(
            path, data=json.dumps(data), content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.tokens[role],
        )

    def publication(self, project, **changes):
        return ProjectPublication.objects.create(
            project=project, title="Oferta sintética QA", description="Requisitos QA",
            vacancies=2, weekly_hours="4.00", opening_date=self.fixture.today,
            closing_date=self.fixture.today + timedelta(days=3), **changes,
        )

    def test_detail_preserves_move_revision_position_and_calendar_contract(self):
        project = self.project()
        task = self.task(project, 1)
        TaskOrder.objects.create(task=task, position=2048)
        listing = self.get(f"/api/projects/{project.pk}/tasks/")[0].json()[0]
        detail = self.get(f"/api/projects/{project.pk}/tasks/{task.pk}/")[0].json()["task"]
        for field in ("move_revision", "manual_position", "calendar_revision", "parent_id", "state_code"):
            self.assertEqual(detail[field], listing[field], field)
        moved = self.send("post", f"/api/projects/{project.pk}/tasks/{task.pk}/move/", {
            "state_code": "EN_CURSO", "before_id": None, "expected_revision": detail["move_revision"],
        })
        self.assertEqual(moved.status_code, 200, moved.content)
        self.assertEqual(moved.json()[0]["calendar_revision"], detail["calendar_revision"])

    def test_consumption_preserves_user_result_and_rejects_reuse_or_wrong_user(self):
        token = issue_stored(self.fixture.worker, "refresh", timezone.now() + timedelta(hours=1))
        with self.assertRaises(InvalidToken):
            consume_stored(token, "refresh", user=self.fixture.admin)
        self.assertIsNone(AuthToken.objects.get(digest=digest_token(token)).consumed_at)
        deadline = timezone.now() + timedelta(minutes=10)
        AuthToken.objects.filter(digest=digest_token(token)).update(expires_at=deadline)
        consumed, expiry = consume_stored(token, "refresh")
        self.assertEqual(consumed.pk, self.fixture.worker.pk)
        self.assertEqual(expiry, deadline)
        with self.assertRaises(InvalidToken):
            consume_stored(token, "refresh")

        another = issue_stored(self.fixture.worker, "refresh", timezone.now() + timedelta(hours=1))
        AuthToken.objects.filter(digest=digest_token(another)).update(expires_at=deadline)
        self.assertEqual(consume_stored(another, "refresh", user=self.fixture.worker)[1], deadline)

    def test_publication_creation_update_state_and_validation_share_endpoint(self):
        project = self.project()
        payload = {"project_id": project.pk, "title": "Oferta QA", "description": "Descripción QA",
                   "vacancies": 2, "weekly_hours": "4.00", "opening_date": str(self.fixture.today),
                   "closing_date": str(self.fixture.today + timedelta(days=2)), "requirements": []}
        created = self.send("post", "/api/publications/", payload)
        self.assertEqual(created.status_code, 201, created.content)
        path = f"/api/publications/{created.json()['id']}/"
        updated = self.send("patch", path, {"title": "Oferta revisada QA", "state": "ABIERTA"})
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(self.get(path)[0].json()["state"], "ABIERTA")
        invalid = self.send("patch", path, {"vacancies": 0})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("vacancies", invalid.json())
        self.assertEqual(self.get(path)[0].json()["vacancies"], 2)
        self.assertEqual(self.send("patch", path, {"state": "CERRADA"}).status_code, 200)
        self.assertEqual(self.send("patch", path, {"state": "ABIERTA"}).status_code, 400)

    def test_publication_visibility_admin_crud_and_worker_denial(self):
        project = self.project()
        opened = self.publication(project, state="ABIERTA")
        self.publication(project, state="BORRADOR")
        self.publication(project, state="CANCELADA")
        worker = self.get("/api/publications/", "worker")[0]
        self.assertEqual(worker.status_code, 200)
        self.assertEqual([item["id"] for item in worker.json()], [opened.pk])
        self.assertEqual(self.get("/api/publications/", "outsider")[0].json(), [])
        self.assertEqual(self.send("patch", f"/api/publications/{opened.pk}/", {"title": "Denegado"}, "worker").status_code, 403)
        self.assertEqual(self.get("/api/publications/catalogs/", "worker")[0].json()["projects"], [])
        self.assertEqual(self.get("/api/organization/specialties/", "worker")[0].status_code, 403)

    def test_publication_worker_list_query_count_does_not_grow_per_row(self):
        project = self.project()
        counts = []
        for size in (1, 20):
            for number in range(ProjectPublication.objects.count(), size):
                self.publication(project, state="ABIERTA")
            response, count = self.get("/api/publications/", "worker")
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(len(response.json()), size)
            self.assertTrue(all(not item["applied"] for item in response.json()))
            counts.append(count)
        self.assertEqual(counts[0], counts[1])

    def test_publication_requirement_replacement_rolls_back_with_transaction(self):
        from unittest.mock import patch
        from apps.publications.services import update_publication
        publication = self.publication(self.project())
        with patch("apps.publications.services._replace_requirements", side_effect=RuntimeError("QA rollback")):
            with self.assertRaises(RuntimeError), transaction.atomic():
                update_publication(self.fixture.admin, publication.pk, {"title": "Debe revertirse", "requirements": []})
        publication.refresh_from_db()
        self.assertEqual(publication.title, "Oferta sintética QA")
