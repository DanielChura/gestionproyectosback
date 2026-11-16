from django.db import connection
from django.db.models import Min, Q
from rest_framework.exceptions import ValidationError

from apps.workers.models import Worker
from ..models import TaskActivity, TaskLabel
from .access import project_for_user, task_for_user, tasks_for_user
from .assignees import task_assignee_ids
from .audit import record_activity
from .statuses import configured_states


def activity_values(task):
    states = {state["code"]: state for state in configured_states(task.project)}
    state = states.get(task.state_code, {"code": task.state_code, "name": task.state_code})
    labels = list(TaskLabel.objects.filter(task=task).order_by("id").values("name", "kind", "specialty_id", "specialty__name"))
    for label in labels:
        current = label.pop("specialty__name")
        if current is not None:
            label["name"] = current
    return {
        "name": task.name, "state_code": state, "priority": task.priority,
        "start_date": task.start_date.isoformat(), "due_date": task.due_date.isoformat() if task.due_date else None,
        "calendar_precision": task.calendar_precision,
        "start_time": task.start_time.isoformat() if task.start_time else None,
        "due_time": task.due_time.isoformat() if task.due_time else None,
        "actual_started_at": task.actual_started_at.isoformat() if task.actual_started_at else None,
        "actual_finished_at": task.actual_finished_at.isoformat() if task.actual_finished_at else None,
        "estimated_minutes": task.estimated_minutes,
        "responsibles": list(Worker.objects.filter(pk__in=task_assignee_ids(task)).order_by("pk").values("id", "first_names", "last_names")),
        "labels": labels,
    }


def record_changes(task, before, after):
    for field, value in after.items():
        if before[field] != value and (connection.vendor != "mysql" or field in ("responsibles", "labels")):
            record_activity(task, "changed", field=field, old=before[field], new=value)


def record_state_change(task, before):
    if connection.vendor != "mysql":
        after = activity_values(task)
        record_changes(task, before, after)


def activity_page(user, project_id, task_id, after=0):
    project = project_for_user(user, project_id)
    task = task_for_user(user, project, task_id)
    visible = tasks_for_user(user, project).values("pk")
    events = TaskActivity.objects.filter(project=project, task=task).filter(
        Q(related_task_id__isnull=True) | Q(related_task_id__in=visible)
    )
    grouped = events.values("operation_id").annotate(first_id=Min("id"))
    if after:
        grouped = grouped.filter(first_id__lt=after)
    groups = list(grouped.order_by("-first_id")[:21])
    selected = list(reversed(groups[:20]))
    rows = list(events.filter(operation_id__in=[group["operation_id"] for group in selected])
                .order_by("id").values("id", "operation_id", "actor_id", "actor_name", "action", "field",
                                       "old_value", "new_value", "related_task_id", "created_at"))
    by_operation = {}
    for row in rows:
        by_operation.setdefault(row["operation_id"], []).append(row)
    return {"items": [{"id": group["operation_id"], "events": by_operation[group["operation_id"]]} for group in selected],
            "next_cursor": selected[0]["first_id"] if len(groups) > 20 else None}


def cursor_value(raw):
    try:
        value = int(raw or 0)
    except (ValueError, TypeError):
        raise ValidationError({"cursor": "Cursor no válido"}) from None
    if value < 0 or value > 9223372036854775807:
        raise ValidationError({"cursor": "Cursor no válido"})
    return value
