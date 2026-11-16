import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from time import perf_counter
from unittest.mock import patch

from django.db import connections
from apps.projects.models import Task, TaskActivity
from apps.projects.services import ordering
from .gantt_closure_fixture import closure_snapshot, require_closure_database


def prepare_hierarchy(fixture, credentials):
    require_closure_database()
    project = fixture.assigned_project(name="Jerarquía QA")
    root = fixture.task(project, name="Raíz A QA")
    other = fixture.task(project, name="Raíz B QA")
    last = fixture.task(project, name="Raíz C QA")
    child = fixture.task(project, name="Rama QA", parent_id=root.pk)
    leaf = fixture.task(project, name="Hoja QA", parent_id=child.pk)
    sibling = fixture.task(project, name="Hermana QA", parent_id=root.pk)
    data = dict(project=project.pk, root=root.pk, other=other.pk, last=last.pk, child=child.pk, leaf=leaf.pk, sibling=sibling.pk)
    deep = fixture.assigned_project(name="Veinte niveles QA")
    ancestor = None
    for level in range(20):
        ancestor = fixture.task(deep, name=f"Nivel {level + 1} con nombre largo de jerarquía QA", parent_id=ancestor.pk if ancestor else None)
    data.update(deep_project=deep.pk, deep_task=ancestor.pk)
    credentials["context"]["hierarchy"] = data
    checks = []

    def move(task, parent, before=None, revision=None, state=None, user=None):
        current = fixture.request("get", f"{project.pk}/tasks/{task.pk}/", user=fixture.admin).json()["task"]
        return fixture.request("post", f"{project.pk}/tasks/{task.pk}/move/", dict(mode="hierarchy", parent_id=parent, before_id=before,
            expected_revision=revision or current["move_revision"], state_code=state or current["state_code"]), user=user or fixture.admin)

    baseline = list(Task.objects.filter(project=project).values("id", "state_code", "start_date", "due_date", "calendar_revision"))
    for task, parent in ((root, other.pk), (child, None), (child, leaf.pk), (child, child.pk)):
        count = TaskActivity.objects.count()
        response = move(task, parent)
        if response.status_code != 400 or TaskActivity.objects.count() != count:
            raise RuntimeError("Forbidden hierarchy movement or partial audit")
    foreign = fixture.task(fixture.assigned_project(name="Otro ámbito QA"))
    if move(child, foreign.pk).status_code != 404:
        raise RuntimeError("Cross project destination accepted")
    if move(child, root.pk, state="EN_CURSO").status_code != 400:
        raise RuntimeError("Hierarchy changed state")
    if move(root, None, before=child.pk).status_code != 400:
        raise RuntimeError("Foreign sibling anchor accepted")
    if move(root, None, user=fixture.worker).status_code not in (403, 404):
        raise RuntimeError("Unassigned worker movement accepted")
    checks.append("API rejects root/subtask conversion, cycles, nested targets, cross project, state changes, invalid anchors and unassigned worker")
    response = move(child, other.pk)
    if response.status_code != 200:
        raise RuntimeError(response.content)
    child.refresh_from_db()
    if child.parent_id != other.pk or Task.objects.get(pk=leaf.pk).parent_id != child.pk:
        raise RuntimeError("Branch was not retained")
    if list(Task.objects.filter(project=project).values("id", "state_code", "start_date", "due_date", "calendar_revision")) != baseline:
        raise RuntimeError("Hierarchy modified calendar or state")
    move(child, root.pk)
    checks.append("Atomic parent/order move preserves descendants, dates, states and calendar revisions")
    revision = fixture.request("get", f"{project.pk}/tasks/{root.pk}/", user=fixture.admin).json()["task"]["move_revision"]
    barrier = Barrier(2)
    original = ordering.project_for_user

    def synchronized(*args, **kwargs):
        barrier.wait(timeout=15)
        return original(*args, **kwargs)

    def concurrent(task):
        from django.db import connection
        start = perf_counter()
        with connection.cursor() as cursor:
            cursor.execute("SELECT CONNECTION_ID()")
            identity = cursor.fetchone()[0]
        try:
            result = move(task, None, revision=revision)
            return dict(connection=identity, status=result.status_code, start=start, end=perf_counter())
        finally:
            connections.close_all()

    with patch.object(ordering, "project_for_user", synchronized):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(concurrent, task) for task in (root, other)]
            race = [future.result() for future in futures]
    if sorted(item["status"] for item in race) != [200, 409] or len({item["connection"] for item in race}) != 2 or max(item["start"] for item in race) >= min(item["end"] for item in race):
        raise RuntimeError("Concurrent hierarchy precondition was not demonstrated")
    checks.append("Two overlapping independent MySQL connections yield one success and one 409")
    output = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    (output / "hierarchy-api.json").write_text(json.dumps(dict(checks=checks, race=race), indent=2), encoding="utf-8")
    (output / "hierarchy-baseline.json").write_text(json.dumps(closure_snapshot(), default=str), encoding="utf-8")


def verify_hierarchy():
    require_closure_database()
    output = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    before = json.loads((output / "hierarchy-baseline.json").read_text(encoding="utf-8"))
    ledger = json.loads((output / "hierarchy-ledger.json").read_text(encoding="utf-8"))
    after = json.loads(json.dumps(closure_snapshot(), default=str))
    (output / "hierarchy-final.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    old = {row["id"]: row for row in before["task"]}
    current = {row["id"]: row for row in after["task"]}
    for key, row in old.items():
        for field in row:
            if field in ("updated_at", "name", "parent_id"):
                continue
            if current[key][field] != row[field]:
                raise RuntimeError(f"Unexpected field change {key}:{field}")
        expected = ledger["changed"].get(str(key), {})
        for field in ("parent_id", "name"):
            if current[key][field] != expected.get(field, row[field]):
                raise RuntimeError(f"Unconfirmed hierarchy/name {key}:{field}")
    created = {row["id"]: row for row in ledger["created"]}
    if set(current) != set(old) | set(created):
        raise RuntimeError("Unexpected task count")
    for key, row in created.items():
        if any(str(current[key][field]) != str(row[field]) for field in ("parent_id", "name", "state_code", "start_date", "due_date")):
            raise RuntimeError("Created task mismatch")
    for model in ("taskdependency", "tasklabel", "taskassignee", "taskcalendarintent"):
        if after[model] != before[model]:
            raise RuntimeError(f"Unexpected {model} change")
    old_history = {row["id"]: row for row in before["taskactivity"]}
    new_history = {row["id"]: row for row in after["taskactivity"]}
    if any(new_history.get(key) != row for key, row in old_history.items()):
        raise RuntimeError("History lost")
    (output / "hierarchy-verification.json").write_text(json.dumps(dict(result="Aprobado", tasks=len(current), new_tasks=len(created), new_history=len(new_history)-len(old_history)), indent=2), encoding="utf-8")
    print("Hierarchy, calendars, states, related data and history verified.", flush=True)
