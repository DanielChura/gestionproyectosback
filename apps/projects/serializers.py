from collections.abc import Mapping

from django.utils import timezone
from rest_framework import serializers

from apps.authentication.serializers import StrictSerializer
from apps.pagination import ListQuerySerializer


class ProjectFavoriteSerializer(StrictSerializer):
    is_favorite = serializers.BooleanField()


class LabelSerializer(StrictSerializer):
    name = serializers.CharField(max_length=120)
    kind = serializers.ChoiceField(choices=("technical", "nontechnical"))


class InitialTaskStateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=70)
    category = serializers.ChoiceField(choices=("not_started", "active", "done", "closed"))
    tema_id = serializers.IntegerField(min_value=1)


class ProjectCreateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=180)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")
    end_date = serializers.DateField(required=False, allow_null=True, default=None)
    area_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=1,
        error_messages={"empty": "Selecciona un área", "max_length": "Selecciona exactamente un área"},
    )
    mode = serializers.ChoiceField(choices=("available", "assigned"))
    worker_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=200)
    worker_contribute = serializers.BooleanField(default=False)
    participant_source = serializers.ChoiceField(choices=("workers", "teams"), default="workers")
    team_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=100)
    initial_task_states = InitialTaskStateSerializer(many=True, required=False, allow_empty=False, max_length=50)

    def validate(self, attrs):
        for key in ("area_ids", "worker_ids", "team_ids"):
            if len(attrs[key]) != len(set(attrs[key])):
                raise serializers.ValidationError({key: "No se permiten selecciones duplicadas"})
        if attrs["participant_source"] == "teams" and attrs["worker_ids"] or attrs["participant_source"] == "workers" and attrs["team_ids"]:
            raise serializers.ValidationError({"participant_source": "No envíes selecciones ocultas del otro origen"})
        if attrs["mode"] == "assigned" and not (attrs["worker_ids"] or attrs["team_ids"]):
            raise serializers.ValidationError(
                {"worker_ids": "Selecciona al menos un trabajador para crear con asignados"}
            )
        if attrs["mode"] == "available" and (attrs["worker_ids"] or attrs["team_ids"]):
            raise serializers.ValidationError({"worker_ids": "Publicar por área no asigna participantes"})
        if attrs["end_date"] is not None and attrs["end_date"] < timezone.localdate():
            raise serializers.ValidationError({"end_date": "La fecha final no puede ser anterior al inicio"})
        if "initial_task_states" in attrs:
            names = [item["name"].casefold() for item in attrs["initial_task_states"]]
            if len(names) != len(set(names)):
                raise serializers.ValidationError({"initial_task_states": "No se permiten estados duplicados"})
        return attrs


def validate_labels(labels):
    keys = {(item["name"].casefold(), item["kind"]) for item in labels}
    if len(keys) != len(labels):
        raise serializers.ValidationError({"labels": "No se permiten etiquetas duplicadas"})


def validate_assignee_input(attrs, initial):
    ids = attrs.get("responsible_ids")
    if ids is not None:
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({"responsible_ids": "No se permiten responsables duplicados"})
        if "responsible_id" in initial:
            expected = [] if attrs["responsible_id"] is None else [attrs["responsible_id"]]
            if set(ids) != set(expected):
                raise serializers.ValidationError({"responsible_ids": "Usa solo una forma de indicar los responsables"})


class TaskCreateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=180)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")
    state_code = serializers.CharField(max_length=20)
    priority = serializers.IntegerField(min_value=1, max_value=4, required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    start_date = serializers.DateField(required=False)
    responsible_id = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    responsible_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=200)
    parent_id = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    requirement_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list, max_length=50)
    labels = LabelSerializer(many=True, required=False, default=list, max_length=50)
    specialty_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=50)
    retained_label_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=50)

    def validate(self, attrs):
        validate_assignee_input(attrs, self.initial_data)
        validate_labels(attrs["labels"])
        if len(attrs.get("specialty_ids", [])) != len(set(attrs.get("specialty_ids", []))):
            raise serializers.ValidationError({"specialty_ids": "No se permiten especialidades duplicadas"})
        if len(attrs["requirement_ids"]) != len(set(attrs["requirement_ids"])):
            raise serializers.ValidationError({"requirement_ids": "No se permiten requisitos duplicados"})
        return attrs


class TaskEditSerializer(StrictSerializer):
    expected_calendar_revision = serializers.IntegerField(min_value=1, max_value=9223372036854775807, required=False)
    calendar_intent_id = serializers.UUIDField(required=False)
    estimated_minutes = serializers.IntegerField(min_value=1, max_value=2147483647, allow_null=True, required=False)
    name = serializers.CharField(max_length=180, required=False)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    priority = serializers.IntegerField(min_value=1, max_value=4, required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    start_date = serializers.DateField(required=False)
    responsible_id = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    responsible_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=200)
    requirement_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=50)
    labels = LabelSerializer(many=True, required=False, max_length=50)
    specialty_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=50)
    retained_label_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, max_length=50)

    def validate(self, attrs):
        from .services.calendar import validate_calendar_preconditions
        validate_calendar_preconditions(attrs)
        validate_assignee_input(attrs, self.initial_data)
        if not attrs:
            raise serializers.ValidationError({"detail": "Indica los datos que quieres modificar"})
        if "labels" in attrs:
            validate_labels(attrs["labels"])
        for field in ("specialty_ids", "retained_label_ids"):
            if len(attrs.get(field, [])) != len(set(attrs.get(field, []))):
                raise serializers.ValidationError({field: "No se permiten selecciones duplicadas"})
        ids = attrs.get("requirement_ids", [])
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({"requirement_ids": "No se permiten requisitos duplicados"})
        return attrs


class TaskStateSerializer(StrictSerializer):
    state_code = serializers.CharField(max_length=20)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class TaskMoveSerializer(TaskStateSerializer):
    before_id = serializers.IntegerField(min_value=1, allow_null=True)
    expected_revision = serializers.CharField(min_length=64, max_length=64)


class DependencySerializer(StrictSerializer):
    predecessor_id = serializers.IntegerField(min_value=1)


class TaskRelationSerializer(StrictSerializer):
    target_id = serializers.IntegerField(min_value=1)
    name = serializers.CharField(max_length=70)


class ProjectTaskStatusSerializer(StrictSerializer):
    code = serializers.CharField(max_length=20)
    name = serializers.CharField(max_length=70)
    category = serializers.ChoiceField(choices=("not_started", "active", "done", "closed"), required=False)
    tema_id = serializers.IntegerField(min_value=1, required=False)


class StateReassignmentSerializer(StrictSerializer):
    source = serializers.CharField(max_length=20)
    target = serializers.CharField(max_length=20)
    reason = serializers.CharField(max_length=500, allow_blank=True, default="")


class TemplateStateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=70)
    category = serializers.ChoiceField(choices=("not_started", "active", "done", "closed"))
    tema_id = serializers.IntegerField(min_value=1)


class TaskStatusTemplateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=70)
    states = TemplateStateSerializer(many=True, max_length=50, allow_empty=True)
    revision = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        names = [state["name"].casefold() for state in attrs["states"]]
        if len(names) != len(set(names)):
            raise serializers.ValidationError({"states": "No se permiten nombres duplicados"})
        return attrs


class ProjectTaskConfigurationSerializer(StrictSerializer):
    template = serializers.ChoiceField(choices=("standard", "custom"))
    revision = serializers.CharField(min_length=64, max_length=64)
    states = ProjectTaskStatusSerializer(many=True, required=False, default=list, max_length=50)
    reassignments = StateReassignmentSerializer(many=True, default=list, max_length=50)

    def validate(self, attrs):
        states = attrs["states"]
        if attrs["template"] == "standard" and states:
            raise serializers.ValidationError({"states": "El modelo estándar no acepta estados personalizados"})
        if attrs["template"] == "custom" and not states:
            raise serializers.ValidationError({"states": "Configura al menos un estado"})
        codes = [state["code"] for state in states]
        names = [state["name"].casefold() for state in states]
        if len(codes) != len(set(codes)) or len(names) != len(set(names)):
            raise serializers.ValidationError({"states": "No se permiten códigos ni nombres duplicados"})
        attrs.pop("reassignments", None)
        return attrs


class ProjectListQuerySerializer(ListQuerySerializer):
    project_id = serializers.IntegerField(min_value=1, required=False)
    area_id = serializers.IntegerField(min_value=1, required=False)
    state_code = serializers.CharField(max_length=20, required=False)
    available = serializers.BooleanField(required=False)
    associated = serializers.BooleanField(required=False)


class ProjectMetricsQuerySerializer(StrictSerializer):
    view = serializers.ChoiceField(choices=("all", "charts", "tables"), required=False, default="all")

    def to_internal_value(self, data):
        normalized = dict(data)
        if "mode" in normalized and "view" not in normalized:
            normalized["view"] = normalized["mode"]
        if isinstance(data, Mapping):
            extra = set(data) - set(self.fields) - {"mode"}
            if extra:
                raise serializers.ValidationError({"detail": "La solicitud contiene campos no permitidos"})
        return super().to_internal_value(normalized)


class ChartDatasetSerializer(StrictSerializer):
    label = serializers.CharField(max_length=120)
    data = serializers.ListField(child=serializers.IntegerField(min_value=0))


class ChartSeriesSerializer(StrictSerializer):
    labels = serializers.ListField(child=serializers.CharField(max_length=120))
    datasets = ChartDatasetSerializer(many=True)
    percentages = serializers.ListField(child=serializers.FloatField(), required=False)
    completion_rates = serializers.ListField(child=serializers.FloatField(), required=False)


class ProjectChartsSerializer(StrictSerializer):
    status_distribution = ChartSeriesSerializer()
    category_distribution = ChartSeriesSerializer()
    priority_distribution = ChartSeriesSerializer()
    tasks_by_person = ChartSeriesSerializer()
    completion_by_person = ChartSeriesSerializer()
    timeline = ChartSeriesSerializer()


class StatusTableRowSerializer(StrictSerializer):
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=100)
    count = serializers.IntegerField(min_value=0)
    percentage = serializers.FloatField()
    percent = serializers.FloatField()


class CategoryTableRowSerializer(StrictSerializer):
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=100)
    count = serializers.IntegerField(min_value=0)
    percentage = serializers.FloatField()


class PriorityTableRowSerializer(StrictSerializer):
    code = serializers.IntegerField()
    name = serializers.CharField(max_length=50)
    count = serializers.IntegerField(min_value=0)
    percentage = serializers.FloatField()


class PeopleTableRowSerializer(StrictSerializer):
    name = serializers.CharField(max_length=200)
    total = serializers.IntegerField(min_value=0)
    completed = serializers.IntegerField(min_value=0)
    pending = serializers.IntegerField(min_value=0)
    completion_rate = serializers.FloatField()


class TimelineTableRowSerializer(StrictSerializer):
    date = serializers.CharField(max_length=20)
    created = serializers.IntegerField(min_value=0)
    completed = serializers.IntegerField(min_value=0)
    cumulative_created = serializers.IntegerField(min_value=0)
    cumulative_completed = serializers.IntegerField(min_value=0)


class ProjectTablesSerializer(StrictSerializer):
    status = StatusTableRowSerializer(many=True)
    category = CategoryTableRowSerializer(many=True)
    priority = PriorityTableRowSerializer(many=True)
    people = PeopleTableRowSerializer(many=True)
    timeline = TimelineTableRowSerializer(many=True)


class ProjectSummarySerializer(StrictSerializer):
    total_tasks = serializers.IntegerField(min_value=0)
    completed_tasks = serializers.IntegerField(min_value=0)
    in_progress_tasks = serializers.IntegerField(min_value=0)
    not_started_tasks = serializers.IntegerField(min_value=0)
    closed_tasks = serializers.IntegerField(min_value=0)
    overdue_tasks = serializers.IntegerField(min_value=0)
    completion_percent = serializers.FloatField()
    average_progress = serializers.FloatField()
    estimated_minutes_total = serializers.IntegerField(min_value=0)
    estimated_minutes_completed = serializers.IntegerField(min_value=0)


class ProjectMetricsResponseSerializer(StrictSerializer):
    project_id = serializers.IntegerField(min_value=1)
    project_name = serializers.CharField(max_length=200)
    summary = ProjectSummarySerializer()
    charts = ProjectChartsSerializer(required=False)
    tables = ProjectTablesSerializer(required=False)
