from django.db import transaction
from django.db.models import F, Q
from rest_framework.exceptions import NotFound, ValidationError

from ..models import TaskDependency, TaskRelation
from .access import project_for_user, require_admin, task_for_user, tasks_for_user
from .activity import cursor_value
from .audit import audit_actor, record_activity
from .presentation import task_snapshots
from .statuses import configured_states
from .tasks import editable_project


def detail_context(user, project_id, task_id):
    project = project_for_user(user, project_id)
    task = task_for_user(user, project, task_id)
    parent = tasks_for_user(user, project).select_related("responsible").filter(pk=task.parent_id).first() if task.parent_id else None
    snapshots = {
        item["id"]: item
        for item in task_snapshots([task] + ([parent] if parent else []), project, user, with_order=False)
    }
    return {"task": snapshots[task.pk], "parent": snapshots[parent.pk] if parent else None}


def subtask_page(user, project_id, task_id, cursor=0):
    project = project_for_user(user, project_id)
    task = task_for_user(user, project, task_id)
    tasks = tasks_for_user(user, project).filter(parent=task)
    final = [state["code"] for state in configured_states(project) if state["category"] in ("done", "closed")]
    rows = list(tasks.select_related("responsible").order_by(F("manual_order__position").asc(nulls_last=True), "id")[cursor:cursor + 31])
    return {"items": task_snapshots(rows[:30], project, user), "total": tasks.count(),
            "completed": tasks.filter(state_code__in=final).count(), "next_cursor": cursor + 30 if len(rows) > 30 else None}


def relation_candidates(user, project_id, task_id, query):
    require_admin(user)
    project = project_for_user(user, project_id)
    editable_project(project)
    task = task_for_user(user, project, task_id)
    kind = query.get("kind", "custom")
    text = query.get("q", "").strip()
    if kind not in ("blocks", "blocked_by", "custom") or len(text) > 100:
        raise ValidationError({"detail": "Búsqueda no válida"})
    tasks = tasks_for_user(user, project).exclude(pk=task.pk)
    if kind != "custom":
        edges = list(TaskDependency.objects.filter(project=project).values_list("predecessor_id", "successor_id"))
        graph = {}
        for predecessor, successor in edges:
            source, destination = (successor, predecessor) if kind == "blocks" else (predecessor, successor)
            graph.setdefault(source, set()).add(destination)
        excluded, pending = {task.pk}, [task.pk]
        while pending:
            for destination in graph.get(pending.pop(), set()) - excluded:
                excluded.add(destination)
                pending.append(destination)
        excluded.update(successor if kind == "blocks" else predecessor for predecessor, successor in edges
                        if (predecessor if kind == "blocks" else successor) == task.pk)
        tasks = tasks.exclude(pk__in=excluded)
    cursor = cursor_value(query.get("cursor"))
    if cursor:
        tasks = tasks.filter(pk__lt=cursor)
    rows = list(tasks.filter(name__icontains=text).select_related("responsible").order_by("-id")[:31])
    return {"items": task_snapshots(rows[:30], project, user), "next_cursor": rows[29].pk if len(rows) > 30 else None}


def relation_page(user, project_id, task_id, cursor=0):
    project = project_for_user(user, project_id)
    task = task_for_user(user, project, task_id)
    visible = tasks_for_user(user, project).values("pk")
    dependencies = TaskDependency.objects.filter(project=project).filter(
        Q(predecessor=task, successor_id__in=visible) | Q(successor=task, predecessor_id__in=visible)
    ).select_related("predecessor", "successor").order_by("predecessor_id", "successor_id")
    custom = TaskRelation.objects.filter(project=project).filter(
        Q(left_task=task, right_task_id__in=visible) | Q(right_task=task, left_task_id__in=visible)
    ).select_related("left_task", "right_task").order_by("id")
    dependency_count = dependencies.count()
    items = []
    if cursor < dependency_count:
        for edge in dependencies[cursor:cursor + 31]:
            target = edge.successor if edge.predecessor_id == task.pk else edge.predecessor
            items.append({"id": f"dep:{edge.predecessor_id}:{edge.successor_id}", "kind": "blocks" if edge.predecessor_id == task.pk else "blocked_by",
                          "name": "", "target_id": target.pk, "target_name": target.name,
                          "predecessor_id": edge.predecessor_id, "successor_id": edge.successor_id, "dependency_type": edge.dependency_type, "legacy": edge.dependency_type is None})
    remaining = 31 - len(items)
    if remaining > 0:
        start = max(0, cursor - dependency_count)
        for edge in custom[start:start + remaining]:
            target = edge.right_task if edge.left_task_id == task.pk else edge.left_task
            items.append({"id": f"custom:{edge.pk}", "relation_id": edge.pk, "kind": "custom", "name": edge.name,
                          "target_id": target.pk, "target_name": target.name})
    return {"items": items[:30], "next_cursor": cursor + 30 if len(items) > 30 else None}


def manage_relation(user, project_id, task_id, data=None, relation_id=None):
    require_admin(user)
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        editable_project(project)
        task = task_for_user(user, project, task_id)
        if relation_id is not None:
            relation = TaskRelation.objects.filter(project=project, pk=relation_id).filter(Q(left_task=task) | Q(right_task=task)).first()
            if relation is None:
                raise NotFound("Relación no disponible")
            target = task_for_user(user, project, relation.right_task_id if relation.left_task_id == task.pk else relation.left_task_id)
            name = relation.name
            relation.delete()
            action = "relation_removed"
        else:
            target = task_for_user(user, project, data["target_id"])
            if target.pk == task.pk:
                raise ValidationError({"target_id": "Selecciona otra tarea"})
            name = data["name"].strip()
            if not name:
                raise ValidationError({"name": "Indica el nombre de la relación"})
            left, right = sorted((task.pk, target.pk))
            if TaskRelation.objects.filter(project=project, left_task_id=left, right_task_id=right, name_key=name.casefold()).exists():
                raise ValidationError({"detail": "La relación ya existe"})
            TaskRelation.objects.create(project=project, left_task_id=left, right_task_id=right, name=name, name_key=name.casefold())
            action = "relation_added"
        record_activity(task, action, new={"name": name, "id": target.pk, "target_name": target.name}, related_task_id=target.pk)
        record_activity(target, action, new={"name": name, "id": task.pk, "target_name": task.name}, related_task_id=task.pk)
