from django.db.models import Exists, OuterRef, Q
from rest_framework.exceptions import NotFound, PermissionDenied

from apps.authentication.services.eligibility import account_eligible
from apps.workers.services.areas import authorized_areas
from ..models import Project, ProjectFavorite, ProjectMember, Task, TaskAssignee


def require_admin(user):
    if user.global_role != "ADMINISTRADOR":
        raise PermissionDenied("Esta operación requiere Administrador")


def can_access_project(user, area_id, *, is_member, is_assigned):
    if not account_eligible(user):
        return False
    if user.global_role == "ADMINISTRADOR":
        return True
    return bool(is_member and is_assigned and authorized_areas(user.worker).filter(pk=area_id).exists())


def can_supervise_project(user, area_id, *, is_member, project_role):
    if not account_eligible(user):
        return False
    if user.global_role == "ADMINISTRADOR":
        return True
    return bool(
        is_member
        and project_role in ("RESPONSABLE", "REVISOR")
        and authorized_areas(user.worker).filter(pk=area_id).exists()
    )


def visible_projects(user):
    projects = Project.objects.filter(archived=False)
    if user.global_role == "ADMINISTRADOR":
        return projects
    if user.global_role != "TRABAJADOR":
        raise PermissionDenied("Cuenta no autorizada")
    areas = authorized_areas(user.worker).values_list("pk", flat=True)
    members = ProjectMember.objects.filter(project_id=OuterRef("pk"), worker_id=user.worker_id)
    return (
        projects.annotate(
            is_member=Exists(members.filter(active=True)),
            revoked_member=Exists(members.filter(active=False)),
        )
        .filter(area_id__in=areas)
        .filter(Q(is_member=True) | Q(available=True, confidential=False, revoked_member=False))
    )


def project_for_user(user, project_id, lock=False):
    queryset = visible_projects(user).annotate(
        is_favorite=Exists(ProjectFavorite.objects.filter(project_id=OuterRef("pk"), user=user)),
        has_associated_workers=Exists(ProjectMember.objects.filter(project_id=OuterRef("pk"), active=True)),
    )
    if lock:
        queryset = queryset.select_for_update()
    else:
        queryset = queryset.select_related("area")
    project = queryset.filter(pk=project_id).first()
    if project is None:
        raise NotFound("Proyecto no disponible")
    return project


def tasks_for_user(user, project):
    tasks = Task.objects.filter(project=project, archived=False)
    if user.global_role == "ADMINISTRADOR":
        return tasks
    assignments = TaskAssignee.objects.filter(task_id=OuterRef("pk"), worker_id=user.worker_id)
    return tasks.annotate(is_additional_assignee=Exists(assignments)).filter(
        Q(responsible_id=user.worker_id) | Q(is_additional_assignee=True)
    )


def task_for_user(user, project, task_id, lock=False):
    queryset = tasks_for_user(user, project)
    if lock:
        queryset = queryset.select_for_update()
    task = queryset.filter(pk=task_id).first()
    if task is None:
        raise NotFound("Tarea no disponible")
    return task


def require_task_permission(user, project, task, permission):
    if user.global_role == "ADMINISTRADOR":
        return
    assigned = task.responsible_id == user.worker_id or TaskAssignee.objects.filter(
        task=task, worker_id=user.worker_id,
    ).exists()
    if user.global_role != "TRABAJADOR" or not assigned or not getattr(project, permission):
        raise PermissionDenied("No tienes permiso para modificar esta tarea")
    if not ProjectMember.objects.filter(project=project, worker_id=user.worker_id, active=True).exists():
        raise PermissionDenied("Se requiere participación activa en el proyecto")


def can_create_tasks(user, project):
    if project.state_code in ("FINALIZADO", "CANCELADO"):
        return False
    if user.global_role == "ADMINISTRADOR":
        return True
    return bool(
        user.global_role == "TRABAJADOR"
        and project.worker_create
        and ProjectMember.objects.filter(
            project=project,
            worker_id=user.worker_id,
            active=True,
            worker__active=True,
        ).exists()
    )


def require_task_creation(user, project):
    if not can_create_tasks(user, project):
        raise PermissionDenied("Se requiere permiso de creación y participación activa en el proyecto")
