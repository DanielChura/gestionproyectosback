from django.db.models import Prefetch, prefetch_related_objects
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.teams.models import Team, TeamMember
from apps.workers.services.access import eligible_workers
from ..models import ProjectMember
from .access import project_for_user


def team_options(area_id, *, project=None, ids=None, lock=False):
    eligible = eligible_workers([area_id])
    if project is not None:
        eligible = eligible.filter(pk__in=ProjectMember.objects.filter(project=project, active=True).values("worker_id"))
    eligible_ids = set(eligible.values_list("pk", flat=True))
    members = TeamMember.objects.filter(left_date__isnull=True, joined_date__lte=timezone.localdate()).select_related("worker").order_by("id")
    teams = Team.objects.filter(area_id=area_id, active=True).order_by("id")
    if ids is not None:
        teams = teams.filter(pk__in=ids)
    if lock:
        teams = teams.select_for_update()
    teams = list(teams)
    if lock:
        list(TeamMember.objects.select_for_update().filter(team_id__in=ids).order_by("pk"))
    prefetch_related_objects(teams, Prefetch("members", queryset=members, to_attr="current_members"))
    if ids is not None and {team.pk for team in teams} != set(ids):
        raise ValidationError({"team_ids": "Selecciona equipos activos del área elegida"})
    result = []
    for team in teams:
        people = {}
        for item in team.current_members:
            allowed = item.worker_id in eligible_ids
            if project is None and not allowed:
                continue
            people[item.worker_id] = {
                "id": item.worker_id, "name": f"{item.worker.first_names} {item.worker.last_names}",
                "eligible": allowed,
                "reason": "" if allowed else "No es participante activo y elegible del proyecto",
            }
        result.append({"id": team.pk, "name": team.name, "members": list(people.values()),
                       "eligible_count": sum(person["eligible"] for person in people.values())})
    return result


def project_team_options(user, project_id):
    project = project_for_user(user, project_id)
    if user.global_role != "ADMINISTRADOR":
        return []
    return team_options(project.area_id, project=project)
