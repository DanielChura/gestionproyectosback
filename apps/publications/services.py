from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.organization.models import Specialty
from apps.projects.models import Project, ProjectMember, ProjectState
from apps.projects.services.access import require_admin, visible_projects
from apps.projects.services.audit import audit_actor

from .models import ProjectPublication, PublicationAssignment, PublicationRequirement, PublicationState


TRANSITIONS = {
    PublicationState.DRAFT: {PublicationState.OPEN, PublicationState.CANCELLED},
    PublicationState.OPEN: {PublicationState.CLOSED, PublicationState.CANCELLED},
    PublicationState.CLOSED: set(),
    PublicationState.CANCELLED: set(),
}
CLOSED_PROJECT_STATES = ("FINALIZADO", "CANCELADO")


def _base_queryset():
    return (
        ProjectPublication.objects.select_related("project")
        .prefetch_related("requirements__specialty")
        .annotate(
            occupied=Count(
                "assignments__worker_id",
                distinct=True,
                filter=Q(assignments__cancelled=False),
            )
        )
    )


def publication_queryset(user):
    queryset = _base_queryset()
    if user.global_role == "ADMINISTRADOR":
        return queryset
    if user.global_role != "TRABAJADOR":
        raise PermissionDenied("Cuenta no autorizada")
    today = timezone.localdate()
    return queryset.annotate(applied=Exists(PublicationAssignment.objects.filter(
        publication_id=OuterRef("pk"), worker_id=user.worker_id, cancelled=False,
    ))).filter(
        project_id__in=visible_projects(user).values("pk"),
        state=PublicationState.OPEN,
        opening_date__lte=today,
        closing_date__gte=today,
    )


def publication_for_user(user, publication_id):
    publication = publication_queryset(user).filter(pk=publication_id).first()
    if publication is None:
        raise NotFound("Publicación no disponible")
    return publication


def list_publications(user, params):
    queryset = publication_queryset(user)
    project_id = params.get("project_id", "").strip()
    if project_id:
        try:
            project_id = int(project_id)
        except ValueError:
            raise ValidationError({"project_id": "Selecciona un proyecto válido"}) from None
        if project_id < 1:
            raise ValidationError({"project_id": "Selecciona un proyecto válido"})
        queryset = queryset.filter(project_id=project_id)
    state = params.get("state", "").strip()
    if state:
        if state not in PublicationState.values:
            raise ValidationError({"state": "Estado no válido"})
        queryset = queryset.filter(state=state)
    search = params.get("search", "").strip()
    if len(search) > 100:
        raise ValidationError({"search": "La búsqueda es demasiado larga"})
    if search:
        queryset = queryset.filter(Q(title__icontains=search) | Q(project__name__icontains=search))
    return list(queryset.order_by("-id")[:200])


def publication_catalog(user):
    projects = []
    if user.global_role == "ADMINISTRADOR":
        terminal = ProjectState.objects.filter(terminal=True).values("pk")
        projects = list(
            Project.objects.filter(archived=False)
            .exclude(state_code__in=terminal)
            .order_by("name")
            .values("id", "code", "name")
        )
    return {
        "states": [{"code": code, "name": name} for code, name in PublicationState.choices],
        "levels": [1, 2, 3],
        "specialties": list(Specialty.objects.values("id", "name")),
        "projects": projects,
    }


def publication_snapshot(publication, user):
    occupied = _occupied_count(publication)
    project = publication.project
    admin = user.global_role == "ADMINISTRADOR"
    project_open = not project.archived and project.state_code not in CLOSED_PROJECT_STATES
    editable = admin and project_open and publication.state in (PublicationState.DRAFT, PublicationState.OPEN)
    allowed_states = set(TRANSITIONS.get(publication.state, set())) if admin else set()
    if not project_open:
        allowed_states.discard(PublicationState.OPEN)
    applied = getattr(publication, "applied", None)
    if applied is None and user.global_role == "TRABAJADOR":
        applied = PublicationAssignment.objects.filter(
            publication_id=publication.pk, worker_id=user.worker_id, cancelled=False,
        ).exists()
    return {
        "id": publication.pk,
        "project": {"id": project.pk, "code": project.code, "name": project.name},
        "title": publication.title,
        "description": publication.description,
        "vacancies": publication.vacancies,
        "filled_vacancies": occupied,
        "available_vacancies": max(publication.vacancies - occupied, 0),
        "weekly_hours": str(publication.weekly_hours),
        "opening_date": publication.opening_date,
        "closing_date": publication.closing_date,
        "state": publication.state,
        "requirements": [
            {
                "specialty_id": requirement.specialty_id,
                "specialty_name": requirement.specialty.name,
                "minimum_level": requirement.minimum_level,
            }
            for requirement in publication.requirements.all()
        ],
        "allowed_states": sorted(allowed_states),
        "permissions": {
            "edit": editable,
            "change_state": bool(allowed_states),
        },
        "applied": bool(applied),
        "can_apply": (
            user.global_role == "TRABAJADOR"
            and publication.state == PublicationState.OPEN
            and max(publication.vacancies - _occupied_count(publication), 0) > 0
        ),
    }


def create_publication(user, data):
    require_admin(user)
    requirements = data["requirements"]
    _validate_requirements(requirements)
    opening_date = data["opening_date"]
    closing_date = data["closing_date"]
    _validate_dates(opening_date, closing_date)
    state = data.get("state", PublicationState.DRAFT)
    if state not in TRANSITIONS.get(PublicationState.DRAFT, set()) and state != PublicationState.DRAFT:
        raise ValidationError({"state": "Estado no válido para una publicación nueva"})
    with audit_actor(user), transaction.atomic():
        project = _project_for_management(data["project_id"], lock=True)
        publication = ProjectPublication.objects.create(
            project=project,
            title=data["title"],
            description=data["description"],
            vacancies=data["vacancies"],
            weekly_hours=data["weekly_hours"],
            opening_date=opening_date,
            closing_date=closing_date,
            state=PublicationState.DRAFT,
        )
        if state != PublicationState.DRAFT:
            publication.state = state
            if state == PublicationState.OPEN:
                _project_for_management(project.pk, lock=True)
                _validate_dates(opening_date, closing_date, must_be_current=True)
                if publication.vacancies < 1:
                    raise ValidationError({"state": "No quedan vacantes disponibles"})
            publication.save(update_fields=["state"])
        _replace_requirements(publication, requirements)
    return _reload(publication.pk)


def update_publication(user, publication_id, data):
    require_admin(user)
    with audit_actor(user), transaction.atomic():
        publication = _managed_publication(user, publication_id, lock=True)
        if publication.state not in (PublicationState.DRAFT, PublicationState.OPEN):
            raise ValidationError({"detail": "La publicación ya está cerrada o cancelada"})
        _project_for_management(publication.project_id)

        opening_date = data.get("opening_date", publication.opening_date)
        closing_date = data.get("closing_date", publication.closing_date)
        _validate_dates(opening_date, closing_date, must_be_current=publication.state == PublicationState.OPEN)

        vacancies = data.get("vacancies", publication.vacancies)
        occupied = _occupied_count(publication)
        if vacancies < occupied:
            raise ValidationError(
                {"vacancies": f"Ya hay {occupied} vacantes ocupadas; no puedes reducir por debajo de ese número"}
            )

        requirements = data.get("requirements")
        if requirements is not None:
            _validate_requirements(requirements)
        new_state = data.get("state")
        if new_state is not None and new_state != publication.state:
            if new_state not in TRANSITIONS.get(publication.state, set()):
                raise ValidationError({"state": f"No se puede pasar de {publication.state} a {new_state}"})
            if new_state == PublicationState.OPEN:
                _project_for_management(publication.project_id, lock=True)
                _validate_dates(opening_date, closing_date, must_be_current=True)
                if _occupied_count(publication) >= vacancies:
                    raise ValidationError({"state": "No quedan vacantes disponibles"})
            publication.state = new_state
        for field, value in data.items():
            if field not in ("requirements", "state"):
                setattr(publication, field, value)
        publication.save()
        if requirements is not None:
            _replace_requirements(publication, requirements)
    return _reload(publication.pk)


def _reload(publication_id):
    return _base_queryset().get(pk=publication_id)


def _managed_publication(user, publication_id, lock=False):
    require_admin(user)
    queryset = ProjectPublication.objects.select_related("project")
    if lock:
        queryset = queryset.select_for_update()
    publication = queryset.filter(pk=publication_id).first()
    if publication is None:
        raise NotFound("Publicación no disponible")
    return publication


def _project_for_management(project_id, lock=False):
    queryset = Project.objects.select_for_update() if lock else Project.objects
    project = queryset.filter(pk=project_id).first()
    if project is None:
        raise ValidationError({"project_id": "El proyecto no existe"})
    if project.archived:
        raise ValidationError({"project_id": "El proyecto está archivado"})
    if ProjectState.objects.filter(pk=project.state_code, terminal=True).exists():
        raise ValidationError({"project_id": "El proyecto está cerrado y no admite publicaciones"})
    return project


def _validate_dates(opening_date, closing_date, *, must_be_current=False):
    if closing_date < opening_date:
        raise ValidationError({"closing_date": "La fecha de cierre no puede ser anterior a la apertura"})
    if must_be_current and closing_date < timezone.localdate():
        raise ValidationError({"closing_date": "La fecha de cierre ya pasó"})


def _validate_requirements(requirements):
    ids = [item["specialty_id"] for item in requirements]
    if len(ids) != len(set(ids)):
        raise ValidationError({"requirements": "No se permiten especialidades duplicadas"})
    found = set(Specialty.objects.filter(pk__in=ids).values_list("pk", flat=True))
    if set(ids) - found:
        raise ValidationError({"requirements": "Hay especialidades inexistentes"})


def _replace_requirements(publication, requirements):
    publication.requirements.all().delete()
    PublicationRequirement.objects.bulk_create(
        [
            PublicationRequirement(
                publication=publication,
                specialty_id=item["specialty_id"],
                minimum_level=item["minimum_level"],
            )
            for item in requirements
        ]
    )


def _occupied_count(publication):
    occupied = getattr(publication, "occupied", None)
    if occupied is not None:
        return occupied
    return (
        PublicationAssignment.objects.filter(publication_id=publication.pk, cancelled=False)
        .values("worker_id")
        .distinct()
        .count()
    )


def apply_publication(user, publication_id):
    if user.global_role != "TRABAJADOR":
        raise PermissionDenied("Solo los trabajadores pueden postularse")
    worker = getattr(user, "worker", None)
    if worker is None:
        raise PermissionDenied("Cuenta no autorizada")
    publication = publication_for_user(user, publication_id)

    existing = PublicationAssignment.objects.filter(
        publication=publication, worker=worker, cancelled=False
    ).exists()
    if existing:
        raise ValidationError({"detail": "Ya te has postulado a esta publicacion"})

    worker_specialties = {
        item.specialty_id: item.level for item in worker.worker_specialties.all()
    }
    for requirement in publication.requirements.all():
        if worker_specialties.get(requirement.specialty_id, 0) < requirement.minimum_level:
            raise ValidationError({"detail": "No cumples los requisitos de la publicacion"})

    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT horas_disponibles FROM v_disponibilidad_actual WHERE trabajador_id = %s",
            [worker.pk],
        )
        row = cursor.fetchone()
    available_hours = row[0] if row else 0
    if available_hours < publication.weekly_hours:
        raise ValidationError({"detail": "No tienes horas disponibles suficientes"})

    occupied = _occupied_count(publication)
    if occupied >= publication.vacancies:
        raise ValidationError({"detail": "No quedan vacantes disponibles"})

    with audit_actor(user), transaction.atomic():
        member = ProjectMember.objects.filter(project=publication.project, worker=worker).first()
        if member is None:
            ProjectMember.objects.create(
                project=publication.project, worker=worker, active=True, project_role="COLABORADOR"
            )
        elif not member.active:
            member.active = True
            member.save(update_fields=["active"])
        PublicationAssignment.objects.create(
            publication=publication,
            worker=worker,
            project=publication.project,
            start_date=publication.opening_date,
            end_date=publication.closing_date,
            weekly_hours=publication.weekly_hours,
            cancelled=False,
        )
    return _reload(publication.pk)
