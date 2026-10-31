from django.db.models import Exists, OuterRef, Q

from apps.organization.models import Area
from apps.pagination import cursor_page
from ..models import ProjectMember
from .access import visible_projects
from .projects import project_snapshots


def project_queryset(user, query):
    members = ProjectMember.objects.filter(project_id=OuterRef("pk"), active=True)
    projects = visible_projects(user).select_related("area").annotate(has_associated_workers=Exists(members))
    for word in query["search"].split():
        projects = projects.filter(Q(name__icontains=word) | Q(code__icontains=word) | Q(area__name__icontains=word))
    for field in ("area_id", "state_code", "available"):
        if field in query:
            projects = projects.filter(**{field: query[field]})
    if "project_id" in query:
        projects = projects.filter(pk=query["project_id"])
    if "associated" in query:
        projects = projects.filter(has_associated_workers=query["associated"])
    return projects


def project_page(user, query):
    return cursor_page(
        project_queryset(user, query), query=query, user=user, resource="projects",
        serialize=lambda rows: project_snapshots(rows, user),
    )


def project_area_page(user, query):
    projects = project_queryset(user, query)
    areas = Area.objects.filter(pk__in=projects.values("area_id"))
    return cursor_page(
        areas, query=query, user=user, resource="project-areas",
        serialize=lambda rows: [{"id": area.pk, "name": area.name} for area in rows],
    )
