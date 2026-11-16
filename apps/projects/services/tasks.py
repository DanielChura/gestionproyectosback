from collections import defaultdict
from django.db import connection, models, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from ..models import ProjectRequirement, Task, TaskDependency, TaskLabel, TaskTransition
from ..serializers import validate_labels
from .access import project_for_user, require_admin, require_task_creation, require_task_permission, supervises_project, task_for_user
from .audit import audit_actor, record_activity
from .activity import activity_values, record_changes, record_state_change
from .statuses import require_configured_state
from .state_categories import definition_rules, state_rule
from .assignees import primary_assignee, requested_assignees, save_assignees, validate_assignees
from .dependency_undo import read_undo, restoring_legacy
from .scheduling import SCHEDULE_FIELDS, calendar_candidate, execution_candidate, prepare_state_change, validate_family, validate_incident


def editable_project(project):
    if project.state_code in ("FINALIZADO", "CANCELADO"):
        raise ValidationError({"detail": "El proyecto está cerrado"})


def validate_dates(project, start_date, due_date, parent=None, task=None):
    if due_date and (due_date < start_date or (project.end_date and due_date > project.end_date)):
        raise ValidationError({"due_date": "La fecha límite queda fuera del periodo permitido"})
    if project.end_date and start_date > project.end_date:
        raise ValidationError({"start_date": "La tarea debe respetar el fin del proyecto"})
    if project.start_date and start_date < project.start_date:
        raise ValidationError({"start_date": "La tarea debe respetar el inicio del proyecto"})
    if parent and start_date < parent.start_date:
        raise ValidationError({"start_date": "La subtarea debe respetar el inicio de su tarea padre"})
    if parent and parent.due_date and (start_date > parent.due_date or due_date and due_date > parent.due_date):
        raise ValidationError({"due_date": "La subtarea debe quedar dentro del periodo de su tarea padre"})
    if task and due_date and Task.objects.filter(parent=task, archived=False).filter(
        models.Q(due_date__gt=due_date) | models.Q(start_date__gt=due_date)
    ).exists():
        raise ValidationError({"due_date": "La fecha dejaría subtareas fuera del periodo padre"})
    if task and Task.objects.filter(parent=task, archived=False, start_date__lt=start_date).exists():
        raise ValidationError({"start_date": "El inicio dejaría subtareas fuera del periodo padre"})


def save_labels(task, data):
    from .labels import save_specialties
    if "specialty_ids" in data or "retained_label_ids" in data:
        save_specialties(task, data)
        return
    if "labels" not in data and "requirement_ids" not in data:
        return
    ids = data.get(
        "requirement_ids",
        list(TaskLabel.objects.filter(task=task, requirement__isnull=False).values_list("requirement_id", flat=True)),
    )
    own = data.get("labels", list(TaskLabel.objects.filter(task=task, requirement__isnull=True, specialty__isnull=True).values("name", "kind")))
    requirements = list(ProjectRequirement.objects.filter(project=task.project, pk__in=ids))
    if len(requirements) != len(ids):
        raise ValidationError({"requirement_ids": "Los requisitos deben pertenecer al proyecto"})
    labels = [{"name": item.name, "kind": item.kind} for item in requirements] + own
    validate_labels(labels)
    desired = [TaskLabel(task=task, requirement=item, name=item.name, kind=item.kind) for item in requirements]
    desired += [TaskLabel(task=task, **label) for label in own]
    def label_key(label):
        return label.requirement_id, label.name, label.kind
    existing = {label_key(label): label for label in TaskLabel.objects.filter(task=task, specialty__isnull=True)}
    keys = {label_key(label) for label in desired}
    TaskLabel.objects.filter(pk__in=[label.pk for identity, label in existing.items() if identity not in keys]).delete()
    TaskLabel.objects.bulk_create([label for label in desired if label_key(label) not in existing])


def create_task(user, project_id, data):
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        editable_project(project)
        require_task_creation(user, project)
        assignee_ids = requested_assignees(data)
        rule = state_rule(project, data["state_code"])
        if user.global_role != "ADMINISTRADOR":
            if not supervises_project(user, project):
                if set(assignee_ids) - {user.worker_id}:
                    raise PermissionDenied(
                        "Solo el Administrador o el coordinador del proyecto pueden asignar tareas a otros trabajadores"
                    )
                assignee_ids = [user.worker_id]
            if rule != "PENDIENTE":
                raise ValidationError(
                    {"state_code": "Crea la tarea pendiente y usa las transiciones autorizadas para cambiar su estado"}
                )
        require_configured_state(project, data["state_code"])
        parent = None
        if data["parent_id"]:
            parent = Task.objects.filter(project=project, pk=data["parent_id"], archived=False).first()
            if parent is None or state_rule(project, parent.state_code) in ("COMPLETADA", "CANCELADA"):
                raise ValidationError({"parent_id": "La tarea padre no está disponible en este proyecto"})
            require_task_permission(user, project, parent, "worker_create")
        schedule = calendar_candidate(Task(start_date=timezone.localdate(), due_date=None), data) if "schedule" in data else None
        if schedule is not None:
            data = {**data, "start_date": schedule.start_date, "due_date": schedule.due_date}
        today = timezone.localdate()
        start_date = data.get("start_date") or max(today, parent.start_date if parent else project.start_date or today)
        if start_date < today:
            raise ValidationError({"start_date": "Las tareas nuevas deben iniciar hoy o después"})
        if data.get("due_date") and data["due_date"] < today:
            raise ValidationError({"due_date": "La fecha límite de una tarea nueva no puede preceder a hoy"})
        validate_dates(project, start_date, data.get("due_date"), parent=parent)
        validate_assignees(project, assignee_ids, rule,
                           "responsible_ids" if "responsible_ids" in data else "responsible_id")
        execution = execution_candidate(project, Task(state_code="PENDIENTE"), data["state_code"])
        task = Task.objects.create(
            actual_started_at=execution.actual_started_at,
            actual_finished_at=execution.actual_finished_at,
            project=project,
            parent=parent,
            phase_id=parent.phase_id if parent else None,
            name=data["name"],
            description=data.get("description", "") or None,
            state_code=data["state_code"],
            priority=data.get("priority"),
            start_date=start_date,
            due_date=data.get("due_date"),
            calendar_precision=schedule.calendar_precision if schedule else "day",
            start_time=schedule.start_time if schedule else None,
            due_time=schedule.due_time if schedule else None,
            responsible_id=primary_assignee(assignee_ids),
            reason=data["reason"] or None,
            progress=100 if rule == "COMPLETADA" else 0,
        )
        validate_family(project, task)
        save_assignees(task, assignee_ids)
        save_labels(task, data)
        from .ordering import append_position
        append_position(task)
        record_activity(task, "created", new=activity_values(task))
        if parent:
            record_activity(parent, "child_created", new={"id": task.pk, "name": task.name}, related_task_id=task.pk)
    return task


def edit_task(user, project_id, task_id, data):
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        task = task_for_user(user, project, task_id, lock=True)
        require_task_permission(user, project, task, "worker_edit")
        editable_project(project)
        if "responsible_id" in data or "responsible_ids" in data or "description" in data:
            require_admin(user)
        from .calendar import begin_calendar_edit, finish_calendar_edit
        intent, replayed = begin_calendar_edit(user, task, data)
        if replayed:
            return task
        before = activity_values(task)
        estimate = data.get("estimated_minutes")
        if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, int) or not 1 <= estimate <= 2147483647):
            raise ValidationError({"estimated_minutes": "Indica minutos enteros positivos o null"})
        candidate = calendar_candidate(task, data)
        parent = task.parent if task.parent_id else None
        validate_dates(project, candidate.start_date, candidate.due_date, parent=parent, task=task)
        if {"schedule", "start_date", "due_date"} & data.keys():
            validate_family(project, candidate)
            validate_incident(candidate, previous=task)
        assignee_ids = requested_assignees(data, task)
        validate_assignees(project, assignee_ids, state_rule(project, task.state_code),
                           "responsible_ids" if "responsible_ids" in data else "responsible_id")
        calendar_changed = any(getattr(candidate, key) != getattr(task, key) for key in SCHEDULE_FIELDS)
        if intent and not calendar_changed and set(data) <= {"schedule", "start_date", "due_date", "expected_calendar_revision", "calendar_intent_id"}:
            finish_calendar_edit(task, intent)
            return task
        if calendar_changed:
            from .calendar import CalendarConflict
            if task.calendar_revision >= 9223372036854775807:
                raise CalendarConflict("calendar_revision_exhausted", "La revisión de calendario requiere revisión operativa.")
            task.calendar_revision += 1
        task.responsible_id = primary_assignee(assignee_ids, task)
        for field in ("name", "description", "priority", "start_date", "due_date", "estimated_minutes"):
            if field in data:
                setattr(task, field, data[field])
        for field in SCHEDULE_FIELDS:
            setattr(task, field, getattr(candidate, field))
        task.updated_at = timezone.now()
        task.save()
        save_assignees(task, assignee_ids)
        save_labels(task, data)
        record_changes(task, before, activity_values(task))
        if intent:
            task.refresh_from_db(fields=[*SCHEDULE_FIELDS, "calendar_revision", "updated_at"])
            finish_calendar_edit(task, intent)
    return task


def has_supervision(user, project):
    return supervises_project(user, project)


def transition_context(user, project, definitions=None, rules=None, legacy=None, supervision=None, *, preview=False):
    from .statuses import configured_states
    rows = list(Task.objects.filter(project=project).values("id", "parent_id", "state_code", "archived", "responsible_id"))
    tasks = {row["id"]: row for row in rows}
    children, incoming, outgoing = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        children[row["parent_id"]].append(row)
    for predecessor, successor in TaskDependency.objects.filter(project=project).values_list("predecessor_id", "successor_id"):
        incoming[successor].append(predecessor)
        outgoing[predecessor].append(successor)
    blocked_deliverables = set()
    if preview and connection.vendor == "mysql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT v.actividad_id FROM v_estado_entregable v JOIN actividad a ON a.id=v.actividad_id WHERE a.proyecto_id=%s AND v.obligatorio=1 AND v.estado_revision<>'APROBADO'", [project.pk])
            blocked_deliverables = {row[0] for row in cursor.fetchall()}
    return {
        "tasks": tasks,
        "children": children, "incoming": incoming, "outgoing": outgoing,
        "states": {item["code"] for item in (definitions if definitions is not None else configured_states(project))},
        "rules": rules if rules is not None else definition_rules(project),
        "legacy": legacy if legacy is not None else {(item["source"], item["target"]): item for item in TaskTransition.objects.values("source", "target", "reason_required", "supervision_required")},
        "supervision": has_supervision(user, project) if supervision is None else supervision,
        "blocked_deliverables": blocked_deliverables, "preview": preview,
    }


def validate_transition(user, project, task, target, reason, context=None, *, direct=False):
    editable_project(project)
    if context is None:
        require_configured_state(project, target)
    elif target not in context["states"]:
        raise ValidationError({"state_code": "El estado no está habilitado en este proyecto"})
    prepare_state_change(project, task, target)


def apply_state_change(project, task, target, reason, nested=False, before=None, *, direct=False):
    before = before or activity_values(task)
    rules = definition_rules(project)
    origin = rules.get(task.state_code, task.state_code)
    destination = rules.get(target, target)
    candidate = prepare_state_change(project, task, target)
    task.actual_started_at = candidate.actual_started_at
    task.actual_finished_at = candidate.actual_finished_at
    task.state_code = target
    if destination == "COMPLETADA":
        task.progress = 100
    elif destination in ("PENDIENTE", "CANCELADA") or origin in ("COMPLETADA", "CANCELADA"):
        task.progress = 0
    task.updated_at = timezone.now()
    task.save(update_fields=["state_code", "progress", "updated_at", "actual_started_at", "actual_finished_at"])
    record_state_change(task, before)


def change_task_state(user, project_id, task_id, data):
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        task = task_for_user(user, project, task_id)
        require_task_permission(user, project, task, "worker_state")
        validate_transition(user, project, task, data["state_code"], data["reason"])
        if task.state_code != data["state_code"]:
            apply_state_change(project, task, data["state_code"], "")
    return task


def dependency_would_cycle(edges, predecessor, successor):
    neighbors = {}
    for source, target in edges:
        neighbors.setdefault(source, []).append(target)
    visited = set()
    pending = [successor]
    while pending:
        node = pending.pop()
        if node == predecessor:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(neighbors.get(node, []))
    return False


def manage_dependency(user, project_id, task_id, predecessor_id, remove=False, dependency_type=None, *, replace=False, expected_type=None, undo_token=None):
    require_admin(user)
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        editable_project(project)
        successor = task_for_user(user, project, task_id)
        predecessor = task_for_user(user, project, predecessor_id)
        if successor.pk == predecessor.pk:
            raise ValidationError({"detail": "Usa dos tareas diferentes del mismo proyecto"})
        if undo_token:
            dependency_type = read_undo(undo_token, user, project_id, predecessor_id, task_id)
        queryset = TaskDependency.objects.filter(project=project, predecessor=predecessor, successor=successor)
        previous_edge = queryset.first()
        previous_type = previous_edge.dependency_type if previous_edge else None
        if remove:
            if previous_edge is None:
                raise NotFound("Dependencia no disponible")
            queryset.delete()
        else:
            from .calendar import CalendarConflict
            if dependency_type not in ("FS", "SS", "FF", "SF") and not (undo_token and dependency_type is None):
                raise ValidationError({"dependency_type": "Selecciona un tipo explícito: FS, SS, FF o SF."})
            existing = queryset.first()
            if replace:
                if existing is None:
                    raise NotFound("Dependencia no disponible")
                if existing.dependency_type != expected_type:
                    raise CalendarConflict("dependency_revision_conflict", "El tipo de dependencia cambió; consulta la relación antes de convertirla.")
            elif existing is not None:
                raise ValidationError({"detail": "La dependencia ya existe; conviértela explícitamente para cambiar su tipo."})
            edges = TaskDependency.objects.filter(project=project).values_list("predecessor_id", "successor_id")
            if dependency_would_cycle(edges, predecessor.pk, successor.pk):
                raise ValidationError({"detail": "La dependencia generaría un ciclo"})
            if replace:
                queryset.update(dependency_type=dependency_type)
            else:
                with restoring_legacy(bool(undo_token) and dependency_type is None):
                    TaskDependency.objects.create(project=project, predecessor=predecessor, successor=successor, dependency_type=dependency_type)
        Task.objects.filter(pk__in=[predecessor.pk, successor.pk]).update(updated_at=timezone.now())
        if remove:
            dependency_type = previous_type
        action = "dependency_removed" if remove else "dependency_changed" if replace else "dependency_added"
        record_activity(successor, action, old={"type": previous_type} if replace or remove else None, new={"direction": "blocked_by", "id": predecessor.pk, "name": predecessor.name, "type": dependency_type}, related_task_id=predecessor.pk)
        record_activity(predecessor, action, old={"type": previous_type} if replace or remove else None, new={"direction": "blocks", "id": successor.pk, "name": successor.name, "type": dependency_type}, related_task_id=successor.pk)

    return previous_type if remove else dependency_type
