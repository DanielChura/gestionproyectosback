from datetime import date
import json
import os
from pathlib import Path

from django.db import connection

from apps.projects.models import (
    Task, TaskActivity, TaskAssignee, TaskCalendarIntent,
    TaskDependency, TaskLabel, TaskOrder,
)
from apps.projects.services.audit import audit_actor


def require_closure_database():
    if (
        os.environ.get("GM_GANTT_STAGE") != "6"
        or connection.settings_dict["HOST"] != "127.0.0.1"
        or connection.settings_dict["NAME"] != "gm_test_task_status_templates"
    ):
        raise RuntimeError("Requires the isolated Gantt closure database")


def closure_snapshot():
    require_closure_database()
    return {
        model._meta.model_name: list(model.objects.order_by("pk").values())
        for model in (
            Task, TaskActivity, TaskOrder, TaskDependency,
            TaskAssignee, TaskLabel, TaskCalendarIntent,
        )
    }


def prepare_closure(fixture, credentials):
    require_closure_database()
    definitions = [
        ("month", "2026-04-30", "2026-04-30", "move", "2026-05-01", "2026-05-01"),
        ("year", "2025-12-31", "2025-12-31", "move", "2026-01-01", "2026-01-01"),
        ("leap", "2024-02-28", "2024-02-29", "end", "2024-02-28", "2024-03-01"),
        ("spring", "2026-03-07", "2026-03-09", "move", "2026-03-08", "2026-03-10"),
        ("europe-fall", "2026-10-24", "2026-10-26", "move", "2026-10-25", "2026-10-27"),
        ("us-fall", "2026-10-31", "2026-11-02", "start", "2026-11-01", "2026-11-02"),
        ("nz-spring", "2026-09-26", "2026-09-28", "move", "2026-09-27", "2026-09-29"),
    ]
    projects = []
    zones = ("America/Lima", "America/New_York", "Europe/Berlin", "Pacific/Auckland")
    for index, zone in enumerate(zones):
        project = fixture.assigned_project(name=f"Cierre Gantt QA {index}", worker_edit=True)
        project.start_date = date(2020, 1, 1)
        project.end_date = None
        project.save(update_fields=["start_date", "end_date"])
        cases = []
        for key, start, due, mode, expected_start, expected_due in definitions:
            task = fixture.task(
                project, name=f"{key} cierre QA {index}",
                responsible_id=fixture.worker.worker_id if key == "month" else None,
                description="Atributos de cierre conservados",
                labels=[{"name": "Calendario QA", "kind": "technical"}],
            )
            with audit_actor(fixture.admin):
                Task.objects.filter(pk=task.pk).update(
                    start_date=date.fromisoformat(start), due_date=date.fromisoformat(due),
                    estimated_minutes=125,
                )
            task.refresh_from_db()
            cases.append({
                "id": task.pk, "key": key, "name": task.name, "mode": mode,
                "start": start, "due": due,
                "expected_start": expected_start, "expected_due": expected_due,
            })
        unbounded = fixture.task(project, name=f"Sin límite cierre QA {index}", due_date=None)
        chain = []
        parent = None
        for level in range(7):
            task = fixture.task(
                project, name=f"Nivel {level} cierre QA {index}",
                parent_id=parent.pk if parent else None,
                state_code=("PENDIENTE", "EN_CURSO", "BLOQUEADA", "EN_REVISION")[level % 4],
                due_date=None if level == 6 else str(fixture.today),
            )
            chain.append(task.pk)
            parent = task
        with audit_actor(fixture.admin):
            TaskDependency.objects.create(
                project=project, predecessor_id=cases[0]["id"], successor_id=cases[1]["id"],
            )
        projects.append({
            "id": project.pk, "name": project.name, "zone": zone,
            "cases": cases, "null": unbounded.pk, "chain": chain,
        })
    credentials["context"]["closure_projects"] = projects
    target = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    (target / "closure-baseline.json").write_text(
        json.dumps(closure_snapshot(), default=str, indent=2), encoding="utf-8",
    )


def verify_migration_state():
    from django.apps import apps
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
    from django.db.migrations.state import ProjectState
    from django.utils import translation

    loader = MigrationLoader(None, ignore_no_migrations=True)
    questioner = NonInteractiveMigrationQuestioner(dry_run=True)
    with translation.override(None):
        detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps), questioner)
        changes = detector.changes(graph=loader.graph)
    leaves = [node for node in loader.graph.leaf_nodes() if node[0] == "proyectos"]
    if changes or leaves != [("proyectos", "0017_task_calendar_guard")]:
        raise RuntimeError(f"Unexpected model/migration state: {sorted(changes)} {leaves}")
    return {"model_state_changes": [], "project_leaves": leaves, "applied_or_generated": False}


def verify_closure():
    require_closure_database()
    target = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    before = json.loads((target / "closure-baseline.json").read_text(encoding="utf-8"))
    after = json.loads(json.dumps(closure_snapshot(), default=str))
    ledger = json.loads((target / "closure-ledger.json").read_text(encoding="utf-8"))
    expected = {int(key): values for key, values in ledger["final_calendars"].items()}
    originals = {row["id"]: row for row in before["task"]}
    current = {row["id"]: row for row in after["task"]}
    if set(originals) != set(current):
        raise RuntimeError("Closure changed the task collection")
    for task_id, row in current.items():
        old = originals[task_id]
        for field in row.keys() - {"start_date", "due_date", "calendar_revision", "updated_at"}:
            if row[field] != old[field]:
                raise RuntimeError(f"Unexpected noncalendar change: {task_id} {field}")
        if row != old and task_id not in expected:
            raise RuntimeError("Unrecorded closure mutation")
        if task_id in expected:
            for field in ("start_date", "due_date", "calendar_revision"):
                if str(row[field]) != str(expected[task_id][field]):
                    raise RuntimeError(f"Wrong final calendar: {task_id} {field}")
    for collection in ("taskorder", "taskdependency", "taskassignee", "tasklabel"):
        if before[collection] != after[collection]:
            raise RuntimeError(f"Closure changed {collection}")
    prior = {row["id"]: row for row in before["taskactivity"]}
    final_history = {row["id"]: row for row in after["taskactivity"]}
    if any(final_history.get(key) != value for key, value in prior.items()):
        raise RuntimeError("Prior audit data changed")
    changes = [row for key, row in final_history.items() if key not in prior]
    accepted = {
        (item["task_id"], item["intent_id"]): item
        for item in ledger["accepted_intents"]
    }
    previous_intents = {row["id"]: row for row in before["taskcalendarintent"]}
    intentions = []
    for row in after["taskcalendarintent"]:
        if row["id"] in previous_intents:
            if row != previous_intents[row["id"]]:
                raise RuntimeError("Prior calendar intention changed")
        else:
            intentions.append(row)
    persisted = {(row["task_id"], row["key"]) for row in intentions}
    if persisted != set(accepted) or any(row["status"] != "succeeded" for row in intentions):
        raise RuntimeError("Intentions differ from browser confirmations")
    groups = {}
    for event in changes:
        if event["task_id"] not in expected or event["field"] not in ("start_date", "due_date"):
            raise RuntimeError("Unexpected closure audit event")
        groups.setdefault((event["task_id"], event["operation_id"]), []).append(event)
    for task_id in expected:
        writes = sum(row["task_id"] == task_id for row in intentions)
        if current[task_id]["calendar_revision"] != originals[task_id]["calendar_revision"] + writes:
            raise RuntimeError("Revision does not match the number of logical writes")
        entries = [row for row in changes if row["task_id"] == task_id]
        fields = sorted(field for item in accepted.values() if item["task_id"] == task_id for field in item["fields"])
        if sorted(row["field"] for row in entries) != fields:
            raise RuntimeError("Duplicate or missing calendar audit event")
    if len(groups) != len(intentions):
        raise RuntimeError("Calendar operation audit grouping differs")
    (target / "closure-final.json").write_text(
        json.dumps(after, indent=2), encoding="utf-8",
    )
    migration_state = verify_migration_state()
    (target / "closure-verification.json").write_text(json.dumps({
        "migration_state": migration_state,
        "confirmed_intentions": len(intentions), "audit_events": len(changes),
        "audit_operations": len(groups), "tasks_preserved": len(current),
        "noncalendar_attributes_preserved": True,
        "hierarchy_states_order_dependencies_assignees_labels_preserved": True,
        "previous_history_preserved": True, "no_unrecorded_or_duplicate_writes": True,
    }, indent=2), encoding="utf-8")
    print("Closure calendar ledger, revisions, audit and all unrelated attributes verified.", flush=True)
