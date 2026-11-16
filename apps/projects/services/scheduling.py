from copy import copy
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..models import Task, TaskDependency
from .state_categories import state_rule


CALENDAR_ZONE = ZoneInfo("America/Lima")
TYPES = ("FS", "SS", "FF", "SF")
SCHEDULE_FIELDS = ("start_date", "due_date", "calendar_precision", "start_time", "due_time")


def planned_window(task):
    start = datetime.combine(task.start_date, task.start_time or time(), CALENDAR_ZONE)
    if task.due_date is None:
        return start, None
    if task.calendar_precision == "day":
        return start, datetime.combine(task.due_date + timedelta(days=1), time(), CALENDAR_ZONE)
    end = datetime.combine(task.due_date, task.due_time, CALENDAR_ZONE)
    return start, end + timedelta(days=1) if task.due_time == time() else end


def schedule_value(task):
    start, end = planned_window(task)
    if task.calendar_precision == "day":
        return {"precision": "day", "start": task.start_date.isoformat(),
                "end": task.due_date.isoformat() if task.due_date else None, "timezone": "America/Lima"}
    return {"precision": "hour", "start": start.isoformat(),
            "end": end.isoformat() if end else None, "timezone": "America/Lima"}


def parse_schedule(value):
    precision = value["precision"]
    if precision == "day":
        try:
            start = datetime.strptime(value["start"], "%Y-%m-%d").date()
            end = datetime.strptime(value["end"], "%Y-%m-%d").date() if value["end"] else None
        except (ValueError, TypeError):
            raise ValidationError({"schedule": "Indica fechas YYYY-MM-DD para una tarea de día completo."}) from None
        if end and end < start:
            raise ValidationError({"schedule": "El límite inclusivo no puede preceder al inicio."})
        return dict(start_date=start, due_date=end, calendar_precision="day", start_time=None, due_time=None)
    try:
        def instant(raw):
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError()
            result = result.astimezone(CALENDAR_ZONE)
            if result.second or result.microsecond:
                raise ValueError()
            return result
        start = instant(value["start"])
        end = instant(value["end"]) if value["end"] else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ValidationError({"schedule": "Indica fecha/hora ISO con zona explícita y precisión de minutos."}) from None
    if end and (end - start).total_seconds() < 3600:
        raise ValidationError({"schedule": "Un intervalo horario debe durar al menos una hora; el fin es exclusivo."})
    last = end - timedelta(microseconds=1) if end else None
    return dict(start_date=start.date(), due_date=last.date() if last else None, calendar_precision="hour",
                start_time=start.timetz().replace(tzinfo=None), due_time=end.timetz().replace(tzinfo=None) if end else None)


def calendar_candidate(task, data):
    candidate = copy(task)
    fields = parse_schedule(data["schedule"]) if "schedule" in data else {key: data[key] for key in ("start_date", "due_date") if key in data}
    for key, value in fields.items():
        setattr(candidate, key, value)
    if candidate.calendar_precision == "hour":
        if candidate.due_date is None:
            candidate.due_time = None
        elif candidate.due_time is None:
            raise ValidationError({"schedule": "Esta tarea conserva horas: usa el formulario horario para definir el límite."})
    validate_window(candidate)
    return candidate


def validate_window(task):
    if task.calendar_precision == "day" and (task.start_time is not None or task.due_time is not None):
        raise ValidationError({"schedule": "Una tarea de día completo no admite horas."})
    if task.calendar_precision == "hour" and (task.start_time is None or (task.due_date is None) != (task.due_time is None)):
        raise ValidationError({"schedule": "El calendario horario requiere inicio y un límite coherente o null."})
    start, end = planned_window(task)
    if end and end <= start:
        raise ValidationError({"schedule": "El fin debe ser posterior al inicio."})
    if task.calendar_precision == "hour" and end and end - start < timedelta(hours=1):
        raise ValidationError({"schedule": "El intervalo mínimo es una hora."})


def validate_family(project, task):
    start, end = planned_window(task)
    if task.parent_id:
        parent_start, parent_end = planned_window(Task.objects.get(pk=task.parent_id))
        if start < parent_start or parent_end and (start >= parent_end or end and end > parent_end):
            raise ValidationError({"schedule": "La propuesta debe quedar dentro del calendario del padre."})
    for child in Task.objects.filter(parent_id=task.pk, archived=False) if task.pk else []:
        child_start, child_end = planned_window(child)
        if child_start < start or end and (child_start >= end or child_end and child_end > end):
            raise ValidationError({"schedule": "La propuesta dejaría subtareas fuera del calendario del padre."})


def execution_rule(project, code):
    if code == "CANCELADA":
        return "cancelled"
    rule = state_rule(project, code)
    return "finished" if rule == "COMPLETADA" else "pending" if rule == "PENDIENTE" else "blocked" if code == "BLOQUEADA" else "started"


def execution_candidate(project, task, target):
    candidate = copy(task)
    if task.state_code == target:
        return candidate
    rule = execution_rule(project, target)
    now = timezone.now()
    if execution_rule(project, task.state_code) == rule:
        candidate.state_code = target
        return candidate
    if rule == "pending":
        candidate.actual_started_at = None
        candidate.actual_finished_at = None
    elif rule == "cancelled":
        candidate.actual_finished_at = None
    elif rule == "finished":
        candidate.actual_started_at = task.actual_started_at or now
        candidate.actual_finished_at = task.actual_finished_at or now
    elif rule == "started":
        candidate.actual_started_at = task.actual_started_at or now
        candidate.actual_finished_at = None
    candidate.state_code = target
    return candidate


def event_pending(task, side):
    actual = task.actual_started_at if side == "S" else task.actual_finished_at
    if actual is not None:
        return False
    rule = execution_rule(task.project, task.state_code)
    return rule in ("pending", "blocked") if side == "S" else rule not in ("finished", "cancelled")


def edge_problems(predecessor, successor, kind, *, planning=True, execution=True):
    if kind is None:
        return []
    issues = []
    if planning:
        a_start, a_end = planned_window(predecessor)
        b_start, b_end = planned_window(successor)
        required_plan = a_end if kind[0] == "F" else a_start
        conditioned_plan = b_start if kind[1] == "S" else b_end
        if required_plan is None or conditioned_plan is None:
            issues.append({"event": "planning", "detail": f"{kind}: define los extremos requeridos antes de modificar el calendario."})
        elif conditioned_plan < required_plan:
            issues.append({"event": "planning", "detail": f"{kind}: el extremo condicionado no puede preceder al requisito. Ajusta las fechas o revisa el tipo de dependencia."})
    if execution:
        required_real = predecessor.actual_finished_at if kind[0] == "F" else predecessor.actual_started_at
        conditioned_real = successor.actual_started_at if kind[1] == "S" else successor.actual_finished_at
        if conditioned_real and (required_real is None or required_real > conditioned_real):
            issues.append({"event": "start" if kind[1] == "S" else "finish", "detail": f"{kind}: falta el evento real del requisito o es posterior al evento condicionado. Completa el requisito antes de continuar."})
        if kind[0] == "F" and predecessor.state_code == "CANCELADA" and conditioned_real:
            issues.append({"event": "execution", "detail": f"{kind}: cancelar el requisito no acredita su finalización satisfactoria."})
    return issues


def validate_edge(predecessor, successor, kind, *, planning=True, execution=True):
    issues = edge_problems(predecessor, successor, kind, planning=planning, execution=execution)
    if issues:
        raise ValidationError({"code": "dependency_constraint", "detail": issues[0]["detail"], "restrictions": issues})


def validate_incident(task, *, previous, events=False):
    edges = TaskDependency.objects.filter(project_id=task.project_id, dependency_type__isnull=False).filter(
        Q(predecessor_id=task.pk) | Q(successor_id=task.pk)
    ).select_related("predecessor__project", "successor__project")
    for edge in edges:
        predecessor = task if edge.predecessor_id == task.pk else edge.predecessor
        successor = task if edge.successor_id == task.pk else edge.successor
        before_predecessor = previous if edge.predecessor_id == task.pk else edge.predecessor
        before_successor = previous if edge.successor_id == task.pk else edge.successor
        if events:
            field = "actual_started_at" if edge.dependency_type[1] == "S" else "actual_finished_at"
            required_field = "actual_finished_at" if edge.dependency_type[0] == "F" else "actual_started_at"
            conditioned_changed = edge.successor_id == task.pk and getattr(task, field) is not None and getattr(task, field) != getattr(previous, field)
            requirement_changed = edge.predecessor_id == task.pk and getattr(task, required_field) != getattr(previous, required_field)
            was_valid = not edge_problems(before_predecessor, before_successor, edge.dependency_type, planning=False)
            if conditioned_changed or requirement_changed and was_valid:
                validate_edge(predecessor, successor, edge.dependency_type, planning=False)
        else:
            was_valid = not edge_problems(before_predecessor, before_successor, edge.dependency_type, execution=False)
            if event_pending(successor, edge.dependency_type[1]) or was_valid:
                validate_edge(predecessor, successor, edge.dependency_type, execution=False)



def prepare_state_change(project, task, target):
    candidate = execution_candidate(project, task, target)
    validate_incident(candidate, previous=task, events=True)
    return candidate


def dependency_contract(edge):
    return {"predecessor_id": edge.predecessor_id, "successor_id": edge.successor_id,
            "type": edge.dependency_type, "legacy": edge.dependency_type is None,
            "conditions": [] if edge.dependency_type is None else [{
                "required_event": "finish" if edge.dependency_type[0] == "F" else "start",
                "conditioned_event": "start" if edge.dependency_type[1] == "S" else "finish",
            }]}
