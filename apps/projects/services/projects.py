from uuid import uuid4

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.authentication.models import User
from apps.organization.models import Area
from apps.organization.services.areas import active_area_ids
from apps.workers.models import Worker
from apps.workers.services.access import eligible_workers

from ..models import Project, ProjectFavorite, ProjectMember, ProjectRequirement, ProjectState, ProjectTaskStatus, ProjectType, ProjectTeam
from .access import can_create_tasks, require_admin
from .audit import audit_actor
from .statuses import status_configuration


def project_areas(project):
    area = getattr(project, "area", None)
    if area is None:
        area = Area.objects.filter(pk=project.area_id).first()
    return [{"id": area.pk, "name": area.name}] if area else []


def eligible_project_workers(project):
    area = getattr(project, "area", None)
    if area is None or not area.active:
        return Worker.objects.none()
    return eligible_workers([area.pk], assume_active=True)


def project_snapshot(project, user, detailed=False, *, areas=None, create_tasks=None):
    admin = user.global_role == "ADMINISTRADOR"
    has_associated_workers = getattr(project, "has_associated_workers", None)
    if has_associated_workers is None:
        has_associated_workers = ProjectMember.objects.filter(project=project, active=True).exists()
    result = {
        "id": project.pk,
        "name": project.name,
        "description": project.description or "",
        "start_date": project.start_date,
        "end_date": project.end_date,
        "state_code": project.state_code,
        "available": project.available,
        "has_associated_workers": has_associated_workers,
        "areas": project_areas(project) if areas is None else areas,
        "worker_edit": project.worker_edit,
        "worker_state": project.worker_state,
        "worker_create": project.worker_create,
        "permissions": {
            "manage": admin and project.state_code not in ("FINALIZADO", "CANCELADO"),
            "create_tasks": can_create_tasks(user, project) if create_tasks is None else create_tasks,
        },
    }
    if detailed:
        is_favorite = getattr(project, "is_favorite", None)
        result["is_favorite"] = (
            is_favorite
            if is_favorite is not None
            else ProjectFavorite.objects.filter(project=project, user=user).exists()
        )
        members = ProjectMember.objects.filter(project=project, active=True, worker__in=eligible_project_workers(project))
        if not admin:
            members = members.filter(worker_id=user.worker_id)
        result["participants"] = [
            {"id": member.worker_id, "name": f"{member.worker.first_names} {member.worker.last_names}"}
            for member in members.select_related("worker")
        ]
        result["requirements"] = list(ProjectRequirement.objects.filter(project=project).values("id", "name", "kind"))
        result["task_states"] = status_configuration(project, user, full=False)
    return result


def project_snapshots(projects, user):
    projects = list(projects)
    role = user.global_role
    memberships = set()
    if role == "TRABAJADOR":
        memberships = set(
            ProjectMember.objects.filter(
                project__in=projects,
                worker_id=user.worker_id,
                active=True,
                worker__active=True,
            ).values_list("project_id", flat=True)
        )
    return [
        project_snapshot(
            project,
            user,
            areas=[{"id": project.area_id, "name": project.area.name}],
            create_tasks=project.state_code not in ("FINALIZADO", "CANCELADO")
            and (
                role == "ADMINISTRADOR"
                or (role == "TRABAJADOR" and project.worker_create and project.pk in memberships)
            ),
        )
        for project in projects
    ]


def create_project(user, data):
    from .teams import team_options
    from .statuses import resolve_states
    require_admin(user)
    if len(data.get("area_ids", [])) != 1:
        raise ValidationError({"area_ids": "Selecciona exactamente un área"})
    area_id = data["area_ids"][0]
    end_date = data.get("end_date")
    with audit_actor(user), transaction.atomic():
        ProjectTaskStatus.objects.exists()
        start_date = timezone.localdate()
        if end_date is not None and end_date < start_date:
            raise ValidationError({"end_date": "La fecha final no puede ser anterior al inicio"})
        list(Area.objects.select_for_update().filter(pk__in=data["area_ids"]).order_by("pk"))
        areas = active_area_ids(data["area_ids"])
        worker_ids = data.get("worker_ids", [])
        team_ids = data.get("team_ids", [])
        if data.get("participant_source", "workers") == "teams":
            teams = team_options(area_id, ids=team_ids, lock=True)
            if any(not team["eligible_count"] for team in teams):
                raise ValidationError({"team_ids": "Cada equipo seleccionado debe aportar integrantes elegibles"})
            worker_ids = sorted({person["id"] for team in teams for person in team["members"] if person["eligible"]})
        list(Worker.objects.select_for_update().filter(pk__in=worker_ids).order_by("pk"))
        list(User.objects.select_for_update().filter(worker_id__in=worker_ids).order_by("pk"))
        workers = list(eligible_workers(areas).filter(pk__in=worker_ids))
        if len(workers) != len(worker_ids):
            raise ValidationError({"worker_ids": "Hay trabajadores no elegibles para el área seleccionada"})
        if (
            not ProjectType.objects.filter(pk="GENERAL").exists()
            or not ProjectState.objects.filter(pk="PLANIFICADO", terminal=False).exists()
        ):
            raise ValidationError(
                {"detail": "El catálogo necesario no está disponible. Revisa las migraciones pendientes"}
            )
        project = Project.objects.create(
            code=f"GM-{uuid4().hex}",
            name=data["name"],
            description=data["description"],
            area_id=area_id,
            type_code="GENERAL",
            start_date=start_date,
            end_date=end_date,
            available=data["mode"] == "available",
            worker_edit=False,
            worker_create=data["worker_contribute"],
            worker_state=data["worker_contribute"],
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        ProjectMember.objects.bulk_create([ProjectMember(project=project, worker=worker) for worker in workers])
        ProjectTeam.objects.bulk_create([ProjectTeam(project=project, team_id=pk) for pk in team_ids])
        if "initial_task_states" in data:
            resolved, _ = resolve_states(project, [{**state, "code": f"draft_{index}"} for index, state in enumerate(data["initial_task_states"])])
            ProjectTaskStatus.objects.bulk_create([ProjectTaskStatus(project=project, position=index, **state) for index, state in enumerate(resolved)])
    return project


def worker_candidates(area_ids, search):
    if len(area_ids) != 1:
        raise ValidationError({"area_ids": "Selecciona exactamente un área"})
    workers = list(eligible_workers(area_ids, search).order_by("last_names", "first_names")[:100])
    counts = dict(
        ProjectMember.objects.filter(worker__in=workers, active=True, project__archived=False)
        .values("worker_id")
        .annotate(total=Count("project_id", distinct=True))
        .values_list("worker_id", "total")
    )
    result = []
    for worker in workers:
        related = counts.get(worker.pk, 0)
        result.append({"id": worker.pk, "name": f"{worker.first_names} {worker.last_names}", "project_count": related})
    return result
