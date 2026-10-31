from collections import defaultdict

from django.utils import timezone

from ..models import Task
from .access import project_for_user, tasks_for_user
from .statuses import configured_states, CATEGORY_NAMES


def _state_category(state_map, code):
    state = state_map.get(code)
    return state["category"] if state else None


def project_chart_data(user, project_id, view="all"):
    project = project_for_user(user, project_id)
    tasks = list(
        tasks_for_user(user, project)
        .select_related("responsible")
        .prefetch_related("taskassignee_set__worker")
    )

    states = configured_states(project)
    state_map = {state["code"]: state for state in states}
    total = len(tasks)

    status_counts = {state["code"]: 0 for state in states}
    for task in tasks:
        if task.state_code in status_counts:
            status_counts[task.state_code] += 1
        else:
            status_counts[task.state_code] = status_counts.get(task.state_code, 0) + 1

    status_labels = [state["name"] for state in states]
    status_data = [status_counts.get(state["code"], 0) for state in states]
    status_percentages = [
        round((count / total) * 100, 2) if total else 0.0 for count in status_data
    ]

    category_counts = {category: 0 for category in ("not_started", "active", "done", "closed")}
    for task in tasks:
        category = _state_category(state_map, task.state_code)
        if category in category_counts:
            category_counts[category] += 1
    category_labels = [CATEGORY_NAMES[category] for category in category_counts]
    category_data = [category_counts[category] for category in category_counts]
    category_percentages = [
        round((count / total) * 100, 2) if total else 0.0 for count in category_data
    ]

    people = {}
    for task in tasks:
        workers = set()
        if task.responsible_id and task.responsible is not None:
            workers.add((task.responsible_id, f"{task.responsible.first_names} {task.responsible.last_names}"))
        for assignee in task.taskassignee_set.all():
            worker = assignee.worker
            if worker is not None:
                workers.add((worker.pk, f"{worker.first_names} {worker.last_names}"))
        completed = _state_category(state_map, task.state_code) in ("done", "closed")
        for worker_id, name in workers:
            entry = people.setdefault(worker_id, {"name": name, "total": 0, "completed": 0})
            entry["total"] += 1
            if completed:
                entry["completed"] += 1

    ordered_people = sorted(people.values(), key=lambda item: (-item["total"], item["name"]))
    person_labels = [item["name"] for item in ordered_people]
    person_totals = [item["total"] for item in ordered_people]
    person_completed = [item["completed"] for item in ordered_people]
    person_pending = [item["total"] - item["completed"] for item in ordered_people]
    person_rates = [
        round((item["completed"] / item["total"]) * 100, 2) if item["total"] else 0.0
        for item in ordered_people
    ]

    priority_names = {1: "Urgente", 2: "Alta", 3: "Normal", 4: "Baja"}
    priority_counts = {code: 0 for code in priority_names}
    for task in tasks:
        code = task.priority if task.priority in priority_counts else 3
        priority_counts[code] += 1
    priority_labels = [priority_names[code] for code in priority_counts]
    priority_data = [priority_counts[code] for code in priority_counts]
    priority_percentages = [
        round((count / total) * 100, 2) if total else 0.0 for count in priority_data
    ]

    today = timezone.localdate()
    completed_tasks = sum(1 for task in tasks if _state_category(state_map, task.state_code) in ("done", "closed"))
    in_progress_tasks = sum(1 for task in tasks if _state_category(state_map, task.state_code) == "active")
    not_started_tasks = sum(1 for task in tasks if _state_category(state_map, task.state_code) == "not_started")
    closed_tasks = sum(1 for task in tasks if _state_category(state_map, task.state_code) == "closed")
    overdue_tasks = sum(
        1
        for task in tasks
        if task.due_date is not None
        and task.due_date < today
        and _state_category(state_map, task.state_code) not in ("done", "closed")
    )
    average_progress = round(sum(float(task.progress or 0) for task in tasks) / total, 2) if total else 0.0
    estimated_total = sum(task.estimated_minutes or 0 for task in tasks)
    estimated_completed = sum(
        task.estimated_minutes or 0
        for task in tasks
        if _state_category(state_map, task.state_code) in ("done", "closed")
    )

    created_by_date = defaultdict(int)
    completed_by_date = defaultdict(int)
    for task in tasks:
        if task.created_at is not None:
            created_by_date[task.created_at.date()] += 1
        if _state_category(state_map, task.state_code) in ("done", "closed") and task.updated_at is not None:
            completed_by_date[task.updated_at.date()] += 1
    timeline_dates = sorted(set(created_by_date) | set(completed_by_date))
    created_cumulative = []
    completed_cumulative = []
    created_running = 0
    completed_running = 0
    for current_date in timeline_dates:
        created_running += created_by_date.get(current_date, 0)
        completed_running += completed_by_date.get(current_date, 0)
        created_cumulative.append(created_running)
        completed_cumulative.append(completed_running)

    response = {
        "project_id": project.pk,
        "project_name": project.name,
        "summary": {
            "total_tasks": total,
            "completed_tasks": completed_tasks,
            "in_progress_tasks": in_progress_tasks,
            "not_started_tasks": not_started_tasks,
            "closed_tasks": closed_tasks,
            "overdue_tasks": overdue_tasks,
            "completion_percent": round((completed_tasks / total) * 100, 2) if total else 0.0,
            "average_progress": average_progress,
            "estimated_minutes_total": estimated_total,
            "estimated_minutes_completed": estimated_completed,
        },
    }

    if view in ("all", "charts"):
        response["charts"] = {
            "status_distribution": {
                "labels": status_labels,
                "datasets": [
                    {
                        "label": "Tareas por estado",
                        "data": status_data,
                    }
                ],
                "percentages": status_percentages,
            },
            "category_distribution": {
                "labels": category_labels,
                "datasets": [
                    {
                        "label": "Tareas por categoria",
                        "data": category_data,
                    }
                ],
                "percentages": category_percentages,
            },
            "tasks_by_person": {
                "labels": person_labels,
                "datasets": [
                    {
                        "label": "Tareas asignadas",
                        "data": person_totals,
                    }
                ],
            },
            "completion_by_person": {
                "labels": person_labels,
                "datasets": [
                    {
                        "label": "Completadas",
                        "data": person_completed,
                    },
                    {
                        "label": "Pendientes",
                        "data": person_pending,
                    },
                ],
                "completion_rates": person_rates,
            },
            "priority_distribution": {
                "labels": priority_labels,
                "datasets": [
                    {
                        "label": "Tareas por prioridad",
                        "data": priority_data,
                    }
                ],
                "percentages": priority_percentages,
            },
            "timeline": {
                "labels": [value.isoformat() for value in timeline_dates],
                "datasets": [
                    {
                        "label": "Creadas (acumulado)",
                        "data": created_cumulative,
                    },
                    {
                        "label": "Completadas (acumulado)",
                        "data": completed_cumulative,
                    },
                ],
            },
        }

    if view in ("all", "tables"):
        response["tables"] = {
            "status": [
                {
                    "code": state["code"],
                    "name": state["name"],
                    "state": state["name"],
                    "count": status_counts.get(state["code"], 0),
                    "percentage": round((status_counts.get(state["code"], 0) / total) * 100, 2) if total else 0.0,
                    "percent": round((status_counts.get(state["code"], 0) / total) * 100, 2) if total else 0.0,
                }
                for state in states
            ],
            "category": [
                {
                    "code": category,
                    "name": CATEGORY_NAMES[category],
                    "count": category_counts[category],
                    "percentage": round((category_counts[category] / total) * 100, 2) if total else 0.0,
                }
                for category in ("not_started", "active", "done", "closed")
            ],
            "priority": [
                {
                    "code": code,
                    "name": priority_names[code],
                    "count": priority_counts[code],
                    "percentage": round((priority_counts[code] / total) * 100, 2) if total else 0.0,
                }
                for code in (1, 2, 3, 4)
            ],
            "people": [
                {
                    "name": item["name"],
                    "total": item["total"],
                    "completed": item["completed"],
                    "pending": item["total"] - item["completed"],
                    "completion_rate": round((item["completed"] / item["total"]) * 100, 2) if item["total"] else 0.0,
                }
                for item in ordered_people
            ],
            "timeline": [
                {
                    "date": current_date.isoformat(),
                    "created": created_by_date.get(current_date, 0),
                    "completed": completed_by_date.get(current_date, 0),
                    "cumulative_created": created_cumulative[index],
                    "cumulative_completed": completed_cumulative[index],
                }
                for index, current_date in enumerate(timeline_dates)
            ],
        }

    return response
