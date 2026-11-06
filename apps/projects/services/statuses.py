import hashlib
import json
import uuid

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from ..models import ProjectTaskStatus, Task, TaskState, TaskStateDefinition, TaskTransition, Tema
from .access import project_for_user, require_admin
from .audit import audit_actor, record_activity
from .state_categories import CATEGORIES, DEFAULT_TEMA_IDS, LEGACY_CATEGORIES, LEGACY_TEMA_IDS


STANDARD_NAMES = {"PENDIENTE": "PENDIENTE", "EN_CURSO": "EN CURSO", "COMPLETADA": "CERRADO"}
STANDARD_ORDER = {code: position for position, code in enumerate(STANDARD_NAMES)}
CATEGORY_NAMES = dict(zip(CATEGORIES, ("No iniciado", "Activo", "Completado", "Cerrado")))


class ConfigurationConflict(APIException):
    status_code = 409
    default_detail = {"revision": "La configuración cambió. Vuelve a cargarla antes de guardar"}


def _tema_map():
    return Tema.objects.in_bulk()


def _tema_payload(tema_id, temas=None):
    if temas is None:
        temas = _tema_map()
    tema = temas.get(tema_id)
    if tema is None:
        return None
    return {"id": tema.id, "color": tema.color, "icono": tema.icono}


def catalog_states():
    temas = _tema_map()
    states = []
    for item in TaskState.objects.exclude(code__in=TaskStateDefinition.objects.values("code")).values("code", "name", "terminal"):
        category = LEGACY_CATEGORIES.get(item["code"], "closed" if item["terminal"] else "active")
        tema_id = LEGACY_TEMA_IDS.get(item["code"], DEFAULT_TEMA_IDS[category])
        states.append({"code": item["code"], "name": STANDARD_NAMES.get(item["code"], item["name"]),
                       "category": category, "tema_id": tema_id, "tema": _tema_payload(tema_id, temas)})
    return sorted(states, key=lambda state: (STANDARD_ORDER.get(state["code"], 3), state["code"]))


def configured_states(project):
    states = list(ProjectTaskStatus.objects.filter(project=project).order_by("position").values("code", "name", "category", "tema_id"))
    if not states:
        return catalog_states()
    temas = _tema_map()
    for state in states:
        state["tema"] = _tema_payload(state["tema_id"], temas)
    return states


def status_configuration(project, user, *, full=True):
    saved = list(ProjectTaskStatus.objects.filter(project=project).order_by("position").values("code", "name", "category", "tema_id"))
    temas = _tema_map()
    for state in saved:
        state["tema"] = _tema_payload(state["tema_id"], temas)
    catalog = catalog_states() if full or not saved else []
    states = saved or catalog
    template = "custom" if saved else "standard"
    revision = hashlib.sha256(json.dumps([template, states], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    admin = user.global_role == "ADMINISTRADOR"
    usage = dict(Task.objects.filter(project=project).values("state_code").annotate(total=Count("id"))
                 .values_list("state_code", "total")) if full and admin else {}
    return {"template": template, "states": states, "available_states": catalog, "revision": revision,
            "usage": usage, "can_configure": admin and project.state_code not in ("FINALIZADO", "CANCELADO")}


def require_configured_state(project, code):
    if code not in {state["code"] for state in configured_states(project)}:
        raise ValidationError({"state_code": "El estado no está habilitado en este proyecto"})
    if not TaskState.objects.filter(code=code).exists():
        raise ValidationError({"state_code": "El estado no existe"})


def validate_status_configuration(project, states):
    codes = {state["code"] for state in states}
    if not set(STANDARD_NAMES) <= codes:
        raise ValidationError({"states": "Conserva Pendiente, En curso y Cerrado"})
    transitions = list(TaskTransition.objects.values_list("source", "target"))
    reachable = {"PENDIENTE"}
    while True:
        expanded = reachable | {target for source, target in transitions if source in reachable and target in codes}
        if expanded == reachable:
            break
        reachable = expanded
    if not {"EN_CURSO", "COMPLETADA"} <= reachable:
        raise ValidationError({"states": "Conserva los estados intermedios de las transiciones vigentes"})


def resolve_states(project, requested, validate=None):
    available = {state["code"]: state for state in configured_states(project) + catalog_states()}
    owned = set(TaskStateDefinition.objects.filter(project=project).values_list("code", flat=True))
    has_custom = False
    resolved = []
    identities = {}
    new_states = []
    new_definitions = []
    for item in requested:
        code = item["code"]
        existing = available.get(code)
        if existing:
            category = item.get("category", existing["category"])
            if category != existing["category"]:
                raise ValidationError({"states": "La categoría de un estado existente no se puede cambiar"})
        elif code.startswith("draft_"):
            category = item.get("category")
        else:
            raise ValidationError({"states": "El estado no pertenece al catálogo ni a este proyecto"})
        if category not in CATEGORIES:
            raise ValidationError({"states": "Indica la categoría funcional de cada estado"})
        actual = "S_" + uuid.uuid4().hex[:18] if existing is None else code
        has_custom = has_custom or code in owned
        identities[code] = actual
        if actual != code:
            new_states.append(TaskState(code=actual, name=actual, terminal=category in ("done", "closed")))
            new_definitions.append(TaskStateDefinition(code=actual, project=project, category=category))
            has_custom = True
        resolved.append({"code": actual, "name": item["name"], "category": category,
                         "tema_id": item.get("tema_id", existing["tema_id"] if existing else DEFAULT_TEMA_IDS[category])})
    resolved = [state for category in CATEGORIES for state in resolved if state["category"] == category]
    if validate is not None:
        validate(resolved)
    categories = {item["category"] for item in resolved}
    if not {"not_started", "active", "closed"} <= categories:
        raise ValidationError({"states": "El flujo necesita al menos un estado No iniciado, Activo y Cerrado"})
    if not has_custom:
        validate_status_configuration(project, resolved)
    TaskState.objects.bulk_create(new_states)
    TaskStateDefinition.objects.bulk_create(new_definitions)
    return resolved, identities


def category_mapping(previous, states, usage):
    old = {state["code"]: state for state in previous}
    current = {state["code"]: state for state in states}
    if set(usage) - old.keys():
        raise ValidationError({"states": "Hay tareas con estados fuera de la configuración vigente. Revisa el flujo antes de aplicar"})
    mapping, errors = {}, {}
    for category in CATEGORIES:
        origins = [state for state in previous if state["category"] == category]
        targets = [state for state in states if state["category"] == category]
        for position, origin in enumerate(origins):
            code = origin["code"]
            if code in current and current[code]["category"] == category:
                mapping[code] = code
            elif targets:
                mapping[code] = targets[min(position, len(targets) - 1)]["code"]
            elif usage.get(code, 0):
                errors[category] = f"Añade un estado en {CATEGORY_NAMES[category]} para conservar las tareas de esta categoría"
    if errors:
        raise ValidationError({"categories": errors})
    return mapping


def reassign_tasks(project, previous, states, mapping):
    old, current = {state["code"]: state for state in previous}, {state["code"]: state for state in states}
    for source, target in sorted(mapping.items(), key=lambda item: item[0] != item[1]):
        comparable = ("code", "name", "category", "tema_id")
        if {key: old[source].get(key) for key in comparable} == {key: current[target].get(key) for key in comparable}:
            continue
        tasks = Task.objects.filter(project=project, state_code=source)
        ids = list(tasks.values_list("pk", flat=True)) if connection.vendor != "mysql" or source == target else []
        tasks.update(state_code=target, updated_at=timezone.now())
        for task_id in ids:
            record_activity(Task(pk=task_id, project_id=project.pk), "changed", field="state_code", old=old[source], new=current[target])


def configure_task_states(user, project_id, data):
    require_admin(user)
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        if project.state_code in ("FINALIZADO", "CANCELADO"):
            raise ValidationError({"detail": "No se puede configurar un proyecto cerrado"})
        previous = status_configuration(project, user)
        if data["revision"] != previous["revision"]:
            raise ConfigurationConflict()
        requested = data["states"] if data["template"] == "custom" else catalog_states()
        mapping = {}

        def validate(states):
            mapping.update(category_mapping(previous["states"], states, previous["usage"]))

        if connection.vendor == "mysql":
            with connection.cursor() as cursor:
                cursor.execute("SET @gm_estados_anteriores = %s", [json.dumps({state["code"]: state for state in previous["states"]})])
        states, _ = resolve_states(project, requested, validate)
        ProjectTaskStatus.objects.filter(project=project).delete()
        ProjectTaskStatus.objects.bulk_create([
            ProjectTaskStatus(project=project, position=position, **state) for position, state in enumerate(states)
        ])
        reassign_tasks(project, previous["states"], states, mapping)
        if data["template"] == "standard":
            ProjectTaskStatus.objects.filter(project=project).delete()
        from .access import tasks_for_user
        from .presentation import task_snapshots
        result = status_configuration(project, user)
        result["tasks"] = task_snapshots(tasks_for_user(user, project).select_related("responsible"), project, user)
        return result
