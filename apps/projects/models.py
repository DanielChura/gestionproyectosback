from django.db import models
from django.utils import timezone

from apps.organization.models import Area, Specialty
from apps.teams.models import Team
from apps.workers.models import Worker


class ProjectType(models.Model):
    code = models.CharField(primary_key=True, max_length=30, db_column="codigo")
    name = models.CharField(max_length=100, db_column="nombre")

    class Meta:
        managed = False
        db_table = "tipo_proyecto"


class ProjectState(models.Model):
    code = models.CharField(primary_key=True, max_length=20, db_column="codigo")
    name = models.CharField(max_length=70, db_column="nombre")
    terminal = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "estado_proyecto"


class TaskState(models.Model):
    code = models.CharField(primary_key=True, max_length=20, db_column="codigo")
    name = models.CharField(max_length=70, db_column="nombre")
    terminal = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "estado_actividad"


class TaskTransition(models.Model):
    pk = models.CompositePrimaryKey("source", "target")
    source = models.CharField(max_length=20, db_column="origen")
    target = models.CharField(max_length=20, db_column="destino")
    supervision_required = models.BooleanField(default=False, db_column="requiere_supervision")
    reason_required = models.BooleanField(default=False, db_column="requiere_motivo")

    class Meta:
        managed = False
        db_table = "transicion_actividad"


class Project(models.Model):
    id = models.BigAutoField(primary_key=True)
    code = models.CharField(max_length=40, unique=True, db_column="codigo")
    name = models.CharField(max_length=180, db_column="nombre")
    description = models.TextField(null=True, db_column="descripcion")
    area = models.ForeignKey(Area, db_column="area_id", on_delete=models.DO_NOTHING)
    team = models.ForeignKey(Team, null=True, blank=True, db_column="equipo_id", on_delete=models.DO_NOTHING)
    type_code = models.CharField(max_length=30, db_column="tipo_codigo")
    state_code = models.CharField(max_length=20, default="PLANIFICADO", db_column="estado_codigo")
    priority = models.PositiveSmallIntegerField(default=3, db_column="prioridad")
    start_date = models.DateField(null=True, db_column="fecha_inicio")
    end_date = models.DateField(null=True, db_column="fecha_fin")
    confidential = models.BooleanField(default=False, db_column="confidencial")
    archived = models.BooleanField(default=False, db_column="archivado")
    available = models.BooleanField(default=False, db_column="disponible_por_area")
    worker_edit = models.BooleanField(default=False, db_column="trabajador_edita_tareas")
    worker_state = models.BooleanField(default=False, db_column="trabajador_cambia_estado")
    worker_create = models.BooleanField(default=False, db_column="trabajador_crea_tareas")
    created_at = models.DateTimeField(default=timezone.now, db_column="creado_en")
    updated_at = models.DateTimeField(default=timezone.now, db_column="actualizado_en")

    class Meta:
        managed = False
        db_table = "proyecto"


class ProjectFavorite(models.Model):
    user = models.ForeignKey(
        "autenticacion.User", on_delete=models.CASCADE, db_constraint=False, db_column="usuario_id"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, db_constraint=False, db_column="proyecto_id"
    )

    class Meta:
        db_table = "proyecto_favorito"
        constraints = [
            models.UniqueConstraint(fields=["user", "project"], name="uq_proyecto_favorito_usuario")
        ]


class ProjectMember(models.Model):
    pk = models.CompositePrimaryKey("project_id", "worker_id")
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_column="proyecto_id")
    worker = models.ForeignKey(Worker, on_delete=models.DO_NOTHING, db_column="trabajador_id")
    project_role = models.CharField(max_length=20, default="COLABORADOR", db_column="rol_proyecto")
    active = models.BooleanField(default=True, db_column="activo")

    class Meta:
        managed = False
        db_table = "proyecto_miembro"


class ProjectTeam(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, db_constraint=False, db_column="proyecto_id")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, db_constraint=False, db_column="equipo_id")

    class Meta:
        db_table = "proyecto_equipo"
        constraints = [models.UniqueConstraint(fields=["project", "team"], name="uq_proyecto_equipo")]


class Task(models.Model):
    id = models.BigAutoField(primary_key=True)
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_column="proyecto_id")
    phase_id = models.PositiveBigIntegerField(null=True, db_column="fase_id")
    parent = models.ForeignKey("self", null=True, on_delete=models.DO_NOTHING, db_column="padre_id")
    name = models.CharField(max_length=180, db_column="nombre")
    description = models.TextField(null=True, blank=True, db_column="descripcion")
    responsible = models.ForeignKey(Worker, null=True, on_delete=models.DO_NOTHING, db_column="responsable_id")
    state_code = models.CharField(max_length=20, db_column="estado_codigo")
    priority = models.PositiveSmallIntegerField(null=True, default=3, db_column="prioridad")
    start_date = models.DateField(db_column="fecha_inicio")
    due_date = models.DateField(null=True, db_column="fecha_fin")
    calendar_revision = models.PositiveBigIntegerField(default=1, db_column="calendario_revision")
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True, db_column="duracion_estimada_minutos")
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0, db_column="avance")
    reason = models.CharField(max_length=500, null=True, db_column="motivo_cambio")
    archived = models.BooleanField(default=False, db_column="archivada")
    created_at = models.DateTimeField(default=timezone.now, db_column="creado_en")
    updated_at = models.DateTimeField(default=timezone.now, db_column="actualizado_en")

    class Meta:
        managed = False
        db_table = "actividad"


class TaskAssignee(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, db_constraint=False, db_column="actividad_id")
    worker = models.ForeignKey(Worker, on_delete=models.DO_NOTHING, db_constraint=False, db_column="trabajador_id")

    class Meta:
        db_table = "actividad_responsable_adicional"
        constraints = [models.UniqueConstraint(fields=["task", "worker"], name="uq_actividad_responsable_adicional")]


class TaskOrder(models.Model):
    task = models.OneToOneField(Task, primary_key=True, on_delete=models.CASCADE, db_constraint=False, db_column="actividad_id", related_name="manual_order")
    position = models.DecimalField(max_digits=38, decimal_places=18, db_column="posicion")

    class Meta:
        db_table = "actividad_orden"


class TaskRelation(models.Model):
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_constraint=False, db_column="proyecto_id")
    left_task = models.ForeignKey(Task, on_delete=models.DO_NOTHING, db_constraint=False, related_name="relations_left")
    right_task = models.ForeignKey(Task, on_delete=models.DO_NOTHING, db_constraint=False, related_name="relations_right")
    name = models.CharField(max_length=70)
    name_key = models.CharField(max_length=210)

    class Meta:
        db_table = "actividad_relacion"
        constraints = [
            models.UniqueConstraint(fields=["project", "left_task", "right_task", "name_key"], name="uq_actividad_relacion"),
            models.CheckConstraint(condition=models.Q(left_task_id__lt=models.F("right_task_id")), name="ck_relacion_distinta"),
        ]


class TaskActivity(models.Model):
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_constraint=False, db_column="proyecto_id")
    task = models.ForeignKey(Task, on_delete=models.DO_NOTHING, db_constraint=False, db_column="actividad_id")
    operation_id = models.CharField(max_length=36, db_column="operacion_id")
    actor_id = models.PositiveBigIntegerField(db_column="autor_id")
    actor_name = models.CharField(max_length=225, db_column="autor_nombre")
    action = models.CharField(max_length=32, db_column="accion")
    field = models.CharField(max_length=32, blank=True, default="", db_column="campo")
    old_value = models.JSONField(null=True, db_column="valor_anterior")
    new_value = models.JSONField(null=True, db_column="valor_nuevo")
    related_task_id = models.PositiveBigIntegerField(null=True, db_column="actividad_relacionada_id")
    created_at = models.DateTimeField(default=timezone.now, db_column="creado_en")

    class Meta:
        db_table = "actividad_historial"
        indexes = [models.Index(fields=["task", "operation_id", "id"], name="ix_historial_operacion")]


class TaskDependency(models.Model):
    pk = models.CompositePrimaryKey("project_id", "predecessor_id", "successor_id")
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_column="proyecto_id")
    predecessor = models.ForeignKey(
        Task, related_name="outgoing_dependencies", on_delete=models.DO_NOTHING, db_column="predecesora_id"
    )
    successor = models.ForeignKey(
        Task, related_name="incoming_dependencies", on_delete=models.DO_NOTHING, db_column="sucesora_id"
    )

    class Meta:
        managed = False
        db_table = "dependencia_actividad"


class ProjectRequirement(models.Model):
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_constraint=False, db_column="proyecto_id")
    name = models.CharField(max_length=120, db_column="nombre")
    kind = models.CharField(max_length=20, db_column="clase")

    class Meta:
        db_table = "proyecto_requisito"
        constraints = [models.UniqueConstraint(fields=["project", "name", "kind"], name="uq_proyecto_requisito")]


class ProjectTaskStatus(models.Model):
    pk = models.CompositePrimaryKey("project_id", "code")
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_constraint=False, db_column="proyecto_id")
    code = models.CharField(max_length=20, db_column="estado_codigo")
    name = models.CharField(max_length=70, db_column="nombre")
    position = models.PositiveSmallIntegerField(db_column="orden")
    category = models.CharField(max_length=20, default="active", db_column="categoria")
    color = models.CharField(max_length=7, default="#737373")

    class Meta:
        db_table = "proyecto_estado_actividad"
        ordering = ["position", "code"]
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="uq_proyecto_estado_nombre"),
            models.UniqueConstraint(fields=["project", "position"], name="uq_proyecto_estado_orden"),
        ]


class TaskStateDefinition(models.Model):
    code = models.CharField(primary_key=True, max_length=20, db_column="estado_codigo")
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_constraint=False, db_column="proyecto_id")
    category = models.CharField(max_length=20, db_column="categoria")

    class Meta:
        db_table = "estado_actividad_definicion"


class TaskStatusTemplate(models.Model):
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, db_constraint=False, db_column="proyecto_id")
    name = models.CharField(max_length=70, db_column="nombre")
    states = models.JSONField(db_column="estados")
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "proyecto_plantilla_estados"
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="uq_proyecto_plantilla_nombre")]
        ordering = ["name", "id"]


class TaskLabel(models.Model):
    task = models.ForeignKey(Task, on_delete=models.DO_NOTHING, db_constraint=False, db_column="actividad_id")
    requirement = models.ForeignKey(
        ProjectRequirement, null=True, on_delete=models.DO_NOTHING, db_column="requisito_id"
    )
    name = models.CharField(max_length=120, db_column="nombre")
    kind = models.CharField(max_length=20, db_column="clase")
    specialty = models.ForeignKey(Specialty, null=True, blank=True, on_delete=models.PROTECT, db_constraint=False, db_column="especialidad_id")

    class Meta:
        db_table = "actividad_etiqueta"
        constraints = [models.UniqueConstraint(fields=["task", "name", "kind"], name="uq_actividad_etiqueta")]


class TaskCalendarIntent(models.Model):
    actor = models.ForeignKey("autenticacion.User", on_delete=models.DO_NOTHING, db_constraint=False)
    task = models.ForeignKey(Task, on_delete=models.DO_NOTHING, db_constraint=False)
    operation = models.CharField(max_length=32, default="calendar_patch")
    key = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    expected_revision = models.PositiveBigIntegerField()
    applied_revision = models.PositiveBigIntegerField(null=True)
    status = models.CharField(max_length=12, default="pending")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "actividad_calendario_intento"
        constraints = [models.UniqueConstraint(fields=["actor", "task", "operation", "key"], name="uq_calendario_intento")]
        indexes = [models.Index(fields=["expires_at", "status"], name="ix_calendario_caducidad")]
