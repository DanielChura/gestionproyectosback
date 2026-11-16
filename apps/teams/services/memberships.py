from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.workers.models import Worker
from ..models import Team, TeamMember


@transaction.atomic
def add_team_member(team, *, worker_id, joined_date):
    team = Team.objects.select_for_update().get(pk=team.pk)
    worker = Worker.objects.select_for_update().filter(pk=worker_id).first()
    if worker is None:
        raise ValidationError({"worker_id": "Trabajador inexistente"})
    if not worker.active:
        raise ValidationError({"worker_id": "Trabajador inactivo"})
    previous = TeamMember.objects.select_for_update().filter(team=team, worker=worker)
    if previous.filter(left_date__isnull=True).exists():
        raise ValidationError({"worker_id": "El trabajador ya pertenece al equipo"})
    member = previous.order_by("-id").first()
    if member is None:
        member = TeamMember.objects.create(team=team, worker=worker, joined_date=joined_date)
    else:
        if previous.filter(joined_date=joined_date).exclude(pk=member.pk).exists():
            raise ValidationError({"worker_id": "Ya existe un registro con esa fecha de ingreso"})
        member.joined_date = joined_date
        member.left_date = None
        member.save(update_fields=["joined_date", "left_date"])
    return TeamMember.objects.select_related("worker").get(pk=member.pk)


@transaction.atomic
def remove_team_member(team, member_id):
    team = Team.objects.select_for_update().get(pk=team.pk)
    member = (
        TeamMember.objects.select_for_update()
        .filter(pk=member_id, team=team, left_date__isnull=True).first()
    )
    if member is None:
        raise NotFound("Miembro no disponible")
    if team.active:
        active_members = TeamMember.objects.filter(
            team=team, left_date__isnull=True
        ).count()
        if active_members <= 2:
            raise ValidationError(
                {
                    "detail": (
                        "No se puede retirar al integrante porque el equipo quedaría con menos de "
                        "2 integrantes activos. Agrega otro integrante y vuelve a intentarlo."
                    )
                }
            )
    member.left_date = timezone.localdate()
    member.save(update_fields=["left_date"])
