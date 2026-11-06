from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from ..models import TaskStatusTemplate
from .access import project_for_user, require_admin
from .audit import audit_actor
from .state_categories import DEFAULT_TEMA_IDS


PRESETS = {
    "normal": ("Normal", ["EN PROGRESO"]),
    "kanban": ("Kanban", ["EN PROGRESO", "EN REVISIÓN"]),
    "marketing": ("Marketing", ["CONCEPTO", "EN PROGRESO", "EN EJECUCIÓN", "EN REVISIÓN"]),
    "contenido": ("Contenido", ["LISTO", "REDACTANDO", "APROBACIÓN", "RECHAZADO", "PUBLICADO"]),
    "scrum": ("Scrum", ["PENDIENTE", "EN PROGRESO", "COMPLETADO", "EN REVISIÓN", "ACEPTADO", "RECHAZADO", "BLOQUEADO"]),
}
ACTIVE_TEMA_IDS = {
    "normal": (2,),
    "kanban": (2, 4),
    "marketing": (5, 3, 4, 2),
    "contenido": (5, 3, 3, 4, 2),
    "scrum": (5, 3, 6, 5, 3, 4, 2),
}
ACTIVE_COLORS = ACTIVE_TEMA_IDS


def preset_templates():
    return [
        {"id": key, "name": name, "preset": True, "revision": 1, "states": [
            {"name": "ABIERTO", "category": "not_started", "tema_id": DEFAULT_TEMA_IDS["not_started"]},
            *[{"name": state, "category": "active", "tema_id": ACTIVE_TEMA_IDS[key][index]}
              for index, state in enumerate(active)],
            {"name": "CERRADO", "category": "closed", "tema_id": DEFAULT_TEMA_IDS["closed"]},
        ]}
        for key, (name, active) in PRESETS.items()
    ]


def template_snapshot(template):
    return {"id": template.pk, "name": template.name, "states": template.states,
            "revision": template.revision, "preset": False}


def project_templates(user, project_id):
    project = project_for_user(user, project_id)
    return preset_templates() + [template_snapshot(item) for item in TaskStatusTemplate.objects.filter(project=project)]


def save_template(user, project_id, data, template_id=None):
    require_admin(user)
    with audit_actor(user), transaction.atomic():
        project = project_for_user(user, project_id, lock=True)
        if project.state_code in ("FINALIZADO", "CANCELADO"):
            raise ValidationError({"detail": "El proyecto está cerrado"})
        if data["name"].casefold() in {name.casefold() for name, _ in PRESETS.values()}:
            raise ValidationError({"name": "Usa un nombre distinto al de las plantillas preestablecidas"})
        if TaskStatusTemplate.objects.filter(project=project, name__iexact=data["name"]).exclude(pk=template_id).exists():
            raise ValidationError({"name": "Ya existe una plantilla con este nombre en el proyecto"})
        if template_id is not None:
            template = TaskStatusTemplate.objects.filter(project=project, pk=template_id).first()
            if template is None:
                raise NotFound("Plantilla no disponible en este proyecto")
            if data.get("revision") != template.revision:
                raise ValidationError({"revision": "La plantilla cambió. Recarga las plantillas antes de guardar"})
            template.revision += 1
        else:
            if TaskStatusTemplate.objects.filter(project=project).count() >= 100:
                raise ValidationError({"name": "El proyecto admite hasta 100 plantillas"})
            template = TaskStatusTemplate(project=project)
        template.name = data["name"]
        template.states = [{key: state[key] for key in ("name", "category", "tema_id")} for state in data["states"]]
        template.save()
        return template_snapshot(template)
