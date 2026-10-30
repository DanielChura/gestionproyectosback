from datetime import timedelta
import json
import os
from pathlib import Path

from django.db import connection
from django.utils import timezone

from apps.projects.models import Task, TaskActivity, TaskCalendarIntent, TaskDependency, TaskOrder
from apps.projects.services.audit import audit_actor


def require_isolated():
    if (
        os.environ.get("GM_GANTT_STAGE") != "3"
        or connection.settings_dict["HOST"] != "127.0.0.1"
        or connection.settings_dict["NAME"] != "gm_test_task_status_templates"
    ):
        raise RuntimeError("Requires the isolated Gantt calendar fixture")


def calendar_data():
    require_isolated()
    return {
        "tasks": list(Task.objects.order_by("pk").values()),
        "history": list(TaskActivity.objects.order_by("pk").values()),
        "order": list(TaskOrder.objects.order_by("pk").values()),
        "dependencies": list(TaskDependency.objects.order_by("pk").values()),
        "intents": list(TaskCalendarIntent.objects.order_by("pk").values()),
    }


def prepare_calendar_gestures(fixture, credentials):
    require_isolated()
    project = fixture.assigned_project(name="Gantt edición QA", worker_edit=True, worker_contribute=True)
    project.start_date = fixture.today - timedelta(days=30)
    project.end_date = fixture.today + timedelta(days=60)
    project.save(update_fields=["start_date", "end_date"])
    subject = fixture.task(
        project, name="Mover calendario QA", due_date=str(fixture.today + timedelta(days=4)),
        responsible_id=fixture.worker.worker_id,
    )
    one = fixture.task(project, name="Un día editable QA", due_date=str(fixture.today))
    second = fixture.task(project, name="Otra barra editable QA", due_date=str(fixture.today + timedelta(days=6)))
    parent = fixture.task(project, name="Padre editable QA", due_date=str(fixture.today + timedelta(days=20)))
    child = fixture.task(
        project, name="Hija independiente editable QA", parent_id=parent.pk, state_code="EN_CURSO",
        due_date=str(fixture.today + timedelta(days=10)),
    )
    unbounded = fixture.task(
        project, name="Sin límite editable QA", due_date=None, responsible_id=fixture.worker.worker_id,
    )
    historical = fixture.task(project, name="Histórica editable QA", due_date=str(fixture.today))
    with audit_actor(fixture.admin):
        Task.objects.filter(pk=parent.pk).update(start_date=fixture.today - timedelta(days=10))
        Task.objects.filter(pk=child.pk).update(start_date=fixture.today - timedelta(days=5))
        Task.objects.filter(pk=historical.pk).update(
            start_date=fixture.today - timedelta(days=5), due_date=fixture.today - timedelta(days=5),
            created_at=timezone.now() - timedelta(days=2),
        )
        TaskDependency.objects.create(project=project, predecessor=subject, successor=second)
    credentials["context"].update(
        editing_project=project.pk, editing_subject=subject.pk, editing_one=one.pk,
        editing_second=second.pk, editing_parent=parent.pk, editing_child=child.pk,
        editing_null=unbounded.pk, editing_historical=historical.pk,
    )
    target = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    (target / "calendar-baseline.json").write_text(
        json.dumps(calendar_data(), default=str, indent=2), encoding="utf-8",
    )


def verify_calendar_gestures():
    require_isolated()
    target = Path(os.environ["GM_BROWSER_REPORT_DIR"])
    before = json.loads((target / "calendar-baseline.json").read_text(encoding="utf-8"))
    after = json.loads(json.dumps(calendar_data(), default=str))
    ledger = json.loads((target / "calendar-ledger.json").read_text(encoding="utf-8"))
    expected = {int(key): value for key, value in ledger["final_calendars"].items()}
    original = {row["id"]: row for row in before["tasks"]}
    if {row["id"] for row in after["tasks"]} != set(original):
        raise RuntimeError("Task collection changed")
    changed = []
    for row in after["tasks"]:
        previous = original[row["id"]]
        for field in row.keys() - {"start_date", "due_date", "calendar_revision", "updated_at"}:
            if row[field] != previous[field]:
                raise RuntimeError(f"Unexpected change outside calendar: {row['id']} {field}")
        if row != previous:
            changed.append(row["id"])
            if row["id"] not in expected:
                raise RuntimeError("Unrecorded task mutation")
        if row["id"] in expected:
            values = expected[row["id"]]
            for field in ("start_date", "due_date", "calendar_revision"):
                if str(row[field]) != str(values[field]):
                    raise RuntimeError(f"Final calendar differs: {row['id']} {field}")
    if before["order"] != after["order"] or before["dependencies"] != after["dependencies"]:
        raise RuntimeError("Gantt changed order or dependencies")
    prior_history = {entry["id"]: entry for entry in before["history"]}
    if not set(prior_history).issubset({entry["id"] for entry in after["history"]}):
        raise RuntimeError("Previous audit events were removed")
    new_history = []
    for entry in after["history"]:
        if entry["id"] in prior_history:
            if entry != prior_history[entry["id"]]:
                raise RuntimeError("Previous audit data changed")
        else:
            if entry["task_id"] not in expected or entry["field"] not in ("start_date", "due_date"):
                raise RuntimeError("Unexpected calendar audit field")
            new_history.append(entry)
    successes = {(entry["task_id"], str(entry["key"])): entry for entry in after["intents"]}
    prior_intents = {(entry["task_id"], str(entry["key"])): entry for entry in before["intents"]}
    for key, value in prior_intents.items():
        if successes.pop(key, None) != value:
            raise RuntimeError("Previous intentions changed")
    accepted = {(entry["task_id"], entry["intent_id"]) for entry in ledger["accepted_intents"]}
    if set(successes) != accepted or any(entry["status"] != "succeeded" for entry in successes.values()):
        raise RuntimeError("Persisted intentions differ from accepted operations")
    for task_id in expected:
        writes = sum(entry["task_id"] == task_id for entry in successes.values())
        if int(expected[task_id]["calendar_revision"]) != int(original[task_id]["calendar_revision"]) + writes:
            raise RuntimeError("A replay changed the calendar revision")
    (target / "calendar-final.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    (target / "calendar-verification.json").write_text(json.dumps({
        "changed_tasks": changed, "confirmed_intentions": len(successes),
        "new_history_events": len(new_history), "unrelated_attributes_preserved": True,
        "order_preserved": True, "dependencies_preserved": True,
        "previous_history_preserved": True, "replays_did_not_increment": True,
    }, indent=2), encoding="utf-8")
    print(
        "Calendar writes match the browser ledger; hierarchy, states, "
        "unrelated attributes, order and dependencies preserved.",
        flush=True,
    )
