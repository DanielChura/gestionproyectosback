from django.db import DatabaseError, IntegrityError
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication.security import IsAdministrator
from apps.organization.services.areas import active_area_options
from apps.organization.models import Specialty

from .serializers import (
    DependencySerializer,
    ProjectCreateSerializer,
    ProjectFavoriteSerializer,
    ProjectMetricsQuerySerializer,
    ProjectMetricsResponseSerializer,
    ProjectTaskConfigurationSerializer,
    TaskCreateSerializer,
    TaskEditSerializer,
    TaskStateSerializer,
    TaskStatusTemplateSerializer,
    TaskRelationSerializer,
    TaskMoveSerializer,
)
from .services.access import project_for_user, require_admin, tasks_for_user, visible_projects
from .services.charts import project_chart_data
from .services.favorites import set_project_favorite
from .services.presentation import task_snapshot, task_snapshots
from .services.projects import create_project, project_snapshot, project_snapshots, worker_candidates
from .services.tasks import change_task_state, create_task, edit_task, manage_dependency
from .services.statuses import catalog_states, configure_task_states, status_configuration
from .services.state_templates import project_templates, save_template
from .services.activity import activity_page, cursor_value
from .services.task_detail import detail_context, subtask_page, relation_candidates, relation_page, manage_relation
from .services.teams import team_options, project_team_options
from .services.state_templates import preset_templates
from .services.ordering import move_task


class ProjectApiView(APIView):
    def handle_exception(self, error):
        if isinstance(error, DatabaseError):
            code = 409 if isinstance(error, IntegrityError) or (error.args and error.args[0] == 1644) else 503
            message = (
                "La operación incumple una restricción del proyecto"
                if code == 409
                else "El servicio de proyectos no está disponible. Revisa conexión y migraciones pendientes"
            )
            return Response({"detail": message}, status=code)
        return super().handle_exception(error)


class ProjectCatalogView(ProjectApiView):
    def get(self, request):

        return Response(
            {
                "creation_date": timezone.localdate(),
                "specialties": list(Specialty.objects.values("id", "name")),
                "task_templates": preset_templates(),
                "areas": active_area_options() if request.user.global_role == "ADMINISTRADOR" else [],
                "states": catalog_states(),
                "priorities": [
                    {"code": code, "name": name}
                    for code, name in ((1, "Urgente"), (2, "Alta"), (3, "Normal"), (4, "Baja"))
                ],
            }
        )


class ProjectWorkersView(ProjectApiView):
    permission_classes = [IsAdministrator]

    def get(self, request):
        try:
            ids = [int(value) for value in request.query_params.get("area_ids", "").split(",") if value]
        except ValueError:
            raise ValidationError({"area_ids": "Selecciona áreas válidas"}) from None
        search = request.query_params.get("q", "").strip()
        if len(search) > 100 or len(ids) > 100 or len(set(ids)) != len(ids) or any(value < 1 for value in ids):
            raise ValidationError({"detail": "Filtros no válidos"})
        return Response(worker_candidates(ids, search))


class ProjectListView(ProjectApiView):
    serializer_class = ProjectCreateSerializer

    def get(self, request):
        return Response(project_snapshots(visible_projects(request.user).order_by("-id")[:200], request.user))

    def post(self, request):
        require_admin(request.user)
        serializer = ProjectCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = create_project(request.user, serializer.validated_data)
        return Response(project_snapshot(project, request.user, detailed=True), status=201)


class ProjectDetailView(ProjectApiView):
    def get(self, request, project_id):
        return Response(project_snapshot(project_for_user(request.user, project_id), request.user, detailed=True))


class ProjectChartsView(ProjectApiView):
    serializer_class = ProjectMetricsResponseSerializer

    @extend_schema(
        parameters=[ProjectMetricsQuerySerializer],
        responses={200: ProjectMetricsResponseSerializer},
    )
    def get(self, request, project_id):
        params = request.query_params.dict() if hasattr(request.query_params, "dict") else dict(request.query_params)
        serializer = ProjectMetricsQuerySerializer(data=params)
        serializer.is_valid(raise_exception=True)
        view_mode = serializer.validated_data.get("view", "all")
        data = project_chart_data(request.user, project_id, view=view_mode)
        return Response(ProjectMetricsResponseSerializer(data).data)


class ProjectTeamsView(ProjectApiView):
    permission_classes = [IsAdministrator]

    def get(self, request):
        try:
            area = int(request.query_params.get("area_id", "0"))
        except ValueError:
            raise ValidationError({"area_id": "Selecciona un área válida"}) from None
        if area < 1:
            raise ValidationError({"area_id": "Selecciona un área válida"})
        return Response(team_options(area))


class TaskTeamsView(ProjectApiView):
    def get(self, request, project_id):
        return Response(project_team_options(request.user, project_id))


class TaskMoveView(ProjectApiView):
    def post(self, request, project_id, task_id):
        serializer = TaskMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(move_task(request.user, project_id, task_id, serializer.validated_data))


class ProjectFavoriteView(ProjectApiView):
    def put(self, request, project_id):
        return Response(ProjectFavoriteSerializer(set_project_favorite(request.user, project_id, True)).data)

    def delete(self, request, project_id):
        return Response(ProjectFavoriteSerializer(set_project_favorite(request.user, project_id, False)).data)


class TaskListView(ProjectApiView):
    serializer_class = TaskCreateSerializer

    def get(self, request, project_id):
        project = project_for_user(request.user, project_id)
        return Response(
            task_snapshots(
                tasks_for_user(request.user, project).select_related("responsible").order_by("id"),
                project,
                request.user,
            )
        )

    def post(self, request, project_id):
        serializer = TaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = create_task(request.user, project_id, serializer.validated_data)
        return Response(task_snapshot(task, task.project, request.user), status=201)


class ProjectTaskStatesView(ProjectApiView):
    serializer_class = ProjectTaskConfigurationSerializer

    def get(self, request, project_id):
        project = project_for_user(request.user, project_id)
        return Response(status_configuration(project, request.user))

    def patch(self, request, project_id):
        require_admin(request.user)
        serializer = ProjectTaskConfigurationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(configure_task_states(request.user, project_id, serializer.validated_data))


class TaskStatusTemplatesView(ProjectApiView):
    permission_classes = [IsAdministrator]

    def get(self, request, project_id):
        return Response(project_templates(request.user, project_id))

    def post(self, request, project_id):
        serializer = TaskStatusTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(save_template(request.user, project_id, serializer.validated_data), status=201)


class TaskStatusTemplateView(ProjectApiView):
    permission_classes = [IsAdministrator]

    def put(self, request, project_id, template_id):
        serializer = TaskStatusTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(save_template(request.user, project_id, serializer.validated_data, template_id))


class TaskDetailView(ProjectApiView):
    serializer_class = TaskEditSerializer

    def get(self, request, project_id, task_id):
        return Response(detail_context(request.user, project_id, task_id))

    def patch(self, request, project_id, task_id):
        serializer = TaskEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = edit_task(request.user, project_id, task_id, serializer.validated_data)
        return Response(task_snapshot(task, task.project, request.user))


class TaskStateView(ProjectApiView):
    serializer_class = TaskStateSerializer

    def post(self, request, project_id, task_id):
        serializer = TaskStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = change_task_state(request.user, project_id, task_id, serializer.validated_data)
        return Response(task_snapshot(task, task.project, request.user))


class TaskDependenciesView(ProjectApiView):
    permission_classes = [IsAdministrator]
    serializer_class = DependencySerializer

    def post(self, request, project_id, task_id):
        serializer = DependencySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        manage_dependency(request.user, project_id, task_id, serializer.validated_data["predecessor_id"])
        return Response({"detail": "Dependencia agregada"}, status=201)

    def delete(self, request, project_id, task_id):
        serializer = DependencySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        manage_dependency(request.user, project_id, task_id, serializer.validated_data["predecessor_id"], remove=True)
        return Response(status=204)


class TaskActivityView(ProjectApiView):
    def get(self, request, project_id, task_id):
        return Response(activity_page(request.user, project_id, task_id, cursor_value(request.query_params.get("cursor"))))


class TaskSubtasksView(ProjectApiView):
    def get(self, request, project_id, task_id):
        return Response(subtask_page(request.user, project_id, task_id, cursor_value(request.query_params.get("cursor"))))


class TaskRelationCandidatesView(ProjectApiView):
    def get(self, request, project_id, task_id):
        return Response(relation_candidates(request.user, project_id, task_id, request.query_params))


class TaskRelationsView(ProjectApiView):
    def get(self, request, project_id, task_id):
        return Response(relation_page(request.user, project_id, task_id, cursor_value(request.query_params.get("cursor"))))

    def post(self, request, project_id, task_id):
        serializer = TaskRelationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        manage_relation(request.user, project_id, task_id, serializer.validated_data)
        return Response(status=201)


class TaskRelationView(ProjectApiView):
    def delete(self, request, project_id, task_id, relation_id):
        manage_relation(request.user, project_id, task_id, relation_id=relation_id)
        return Response(status=204)
