from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.authentication.security import IsAdministrator
from apps.organization.serializers import SpecialtySerializer
from apps.organization.services.specialties import specialty_options
from .models import Worker
from .serializers import (
    WorkerSerializer, WorkerSpecialtySerializer,
    WorkerSpecialtyTargetSerializer, WorkerSpecialtyWriteSerializer,
)
from .services.specialties import delete_worker_specialty, upsert_worker_specialty, worker_specialties


class WorkerViewSet(viewsets.ModelViewSet):
    queryset = Worker.objects.select_related("area").prefetch_related("worker_specialties__specialty").order_by("id")
    serializer_class = WorkerSerializer
    permission_classes = [IsAdministrator]

    def get_queryset(self):
        queryset = super().get_queryset()
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
        for word in search.split():
            queryset = queryset.filter(
                Q(first_names__icontains=word) | Q(last_names__icontains=word) | Q(code__icontains=word)
            )
        disponible = self.request.query_params.get("disponible")
        if disponible is not None:
            from apps.projects.models import ProjectMember

            members = ProjectMember.objects.values("worker_id")
            normalized = disponible.strip().lower()
            if normalized in ("true", "1"):
                queryset = queryset.exclude(id__in=members)
            elif normalized in ("false", "0"):
                queryset = queryset.filter(id__in=members)
        return queryset

    @extend_schema(responses=SpecialtySerializer(many=True))
    @action(
        detail=False, methods=["get"], url_path="me/specialty-options",
        permission_classes=[IsAuthenticated],
    )
    def my_specialty_options(self, request):
        return Response(SpecialtySerializer(specialty_options(), many=True).data)

    @action(
        detail=False,
        methods=["get", "post", "delete"],
        url_path="me/specialties",
        permission_classes=[IsAuthenticated],
    )
    def me_specialties(self, request):
        return self._specialties_response(request.user.worker, request)

    @action(
        detail=False,
        methods=["post", "patch", "delete"],
        url_path=r"me/specialties/(?P<specialty_id>\d+)",
        permission_classes=[IsAuthenticated],
    )
    def me_specialty_detail(self, request, specialty_id=None):
        return self._specialties_response(request.user.worker, request, specialty_id)

    @action(detail=True, methods=["get", "post", "delete"], url_path="specialties")
    def specialties(self, request, pk=None):
        return self._specialties_response(self.get_object(), request)

    @action(
        detail=True,
        methods=["post", "patch", "delete"],
        url_path=r"specialties/(?P<specialty_id>\d+)",
    )
    def specialty_detail(self, request, pk=None, specialty_id=None):
        return self._specialties_response(self.get_object(), request, specialty_id)

    def _specialties_response(self, worker, request, specialty_id=None):
        if request.method == "GET":
            return Response(WorkerSpecialtySerializer(worker_specialties(worker), many=True).data)
        serializer_class = (
            WorkerSpecialtyTargetSerializer if request.method == "DELETE"
            else WorkerSpecialtyWriteSerializer
        )
        serializer = serializer_class(data=request.data, context={"specialty_id": specialty_id})
        serializer.is_valid(raise_exception=True)
        if request.method == "DELETE":
            delete_worker_specialty(worker, serializer.validated_data["specialty"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        relation, created = upsert_worker_specialty(worker, **serializer.validated_data)
        return Response(
            WorkerSpecialtySerializer(relation).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
