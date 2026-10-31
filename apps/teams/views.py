from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from apps.authentication.security import IsAdministrator
from apps.projects.services.audit import audit_actor
from apps.workers.services.areas import authorized_areas
from .models import Team, TeamMember
from .serializers import TeamMemberCreateSerializer, TeamMemberSerializer, TeamSerializer
from .services.memberships import add_team_member, remove_team_member


class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.select_related("area").order_by("id")
    serializer_class = TeamSerializer

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError:
            raise ValidationError({"detail": "El equipo está asociado a proyectos. Desactívalo para conservar su historial."}) from None

    def get_permissions(self):
        if self.action in ("list", "retrieve") or (
            self.action == "members" and self.request.method == "GET"
        ):
            return [IsAuthenticated()]
        return [IsAdministrator()]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.global_role != "ADMINISTRADOR":
            queryset = queryset.filter(area__in=authorized_areas(user.worker))
        area_id = self.request.query_params.get("area_id")
        if area_id:
            queryset = queryset.filter(area_id=area_id)
        active = self.request.query_params.get("active")
        if active is not None:
            normalized = active.strip().lower()
            if normalized in ("true", "1"):
                queryset = queryset.filter(active=True)
            elif normalized in ("false", "0"):
                queryset = queryset.filter(active=False)
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(name__icontains=search[:120])
        return queryset.annotate(active_members=Count("members", filter=Q(members__left_date__isnull=True)))

    @action(detail=True, methods=["get", "post"], url_path="members")
    def members(self, request, pk=None):
        team = self.get_object()
        if request.method == "GET":
            members = TeamMember.objects.filter(team=team).select_related("worker").order_by("id")
            if request.query_params.get("all") not in ("1", "true"):
                members = members.filter(left_date__isnull=True)
            serializer = TeamMemberSerializer(members, many=True)
            return Response(serializer.data)
        serializer = TeamMemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with audit_actor(request.user):
            member = add_team_member(team, **serializer.validated_data)
        return Response(TeamMemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"members/(?P<member_id>\d+)")
    def remove_member(self, request, pk=None, member_id=None):
        team = self.get_object()
        with audit_actor(request.user):
            remove_team_member(team, member_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
