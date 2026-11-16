from importlib import import_module
from pathlib import Path

from django.db import DatabaseError, migrations


SQL = Path(__file__).parent / "sql"
OBJECTS = {
    "gm_bi_dependencia_tipo": ("TRIGGER", "0019"),
    "gm_bu_dependencia_tipo": ("TRIGGER", "0019"),
    "gm_validar_dependencias_programacion": ("PROCEDURE", "0019"),
    "sp_validar_estructura_actividad": ("PROCEDURE", "0011"),
}


def body(statement):
    return " ".join(statement[statement.index("BEGIN"):].replace("`", "").split()).lower()


def replace_objects(schema_editor, forward):
    connection = schema_editor.connection
    import_module("apps.projects.migrations.0019_task_scheduling_dependencies").maintained(connection)
    if connection.vendor != "mysql":
        raise RuntimeError("Dependency contract DDL requires isolated or maintained MySQL")
    with connection.cursor() as cursor:
        for name, (kind, previous) in OBJECTS.items():
            if kind == "TRIGGER":
                cursor.execute("SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s", [name])
            else:
                cursor.execute("SELECT ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME=%s", [name])
            current = cursor.fetchone()
            expected = [body((SQL / f"{name}.{version}.sql").read_text(encoding="utf8")) for version in (previous, "0020")]
            if not current or current[0] is None or body(current[0]) not in expected:
                raise RuntimeError(f"Dependency preflight differs for {name}; preserve and review")
        if not forward:
            cursor.execute("SELECT COUNT(*) FROM dependencia_actividad d JOIN actividad a ON a.id=d.predecesora_id JOIN actividad b ON b.id=d.sucesora_id WHERE a.padre_id IS NOT NULL OR b.padre_id IS NOT NULL")
            if cursor.fetchone()[0]:
                raise RuntimeError("Subtask dependencies require the current contract; preserve and review before reversal")
            cursor.execute("""
                SELECT d.tipo,
                    TIMESTAMP(a.fecha_inicio,COALESCE(a.hora_inicio_planificada,'00:00:00')),
                    IF(a.fecha_fin IS NULL,NULL,IF(a.precision_calendario='day' OR TIME_TO_SEC(a.hora_fin_planificada)=0,
                        TIMESTAMP(DATE_ADD(a.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(a.fecha_fin,a.hora_fin_planificada))),
                    TIMESTAMP(b.fecha_inicio,COALESCE(b.hora_inicio_planificada,'00:00:00')),
                    IF(b.fecha_fin IS NULL,NULL,IF(b.precision_calendario='day' OR TIME_TO_SEC(b.hora_fin_planificada)=0,
                        TIMESTAMP(DATE_ADD(b.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(b.fecha_fin,b.hora_fin_planificada))),
                    a.inicio_real_en,a.fin_real_en,b.inicio_real_en,b.fin_real_en,
                    b.estado_codigo,fn_regla_estado_actividad(d.proyecto_id,b.estado_codigo)
                FROM dependencia_actividad d JOIN actividad a ON a.id=d.predecesora_id
                JOIN actividad b ON b.id=d.sucesora_id WHERE d.tipo IS NOT NULL
            """)
            for row in cursor.fetchall():
                kind, *limits, state, rule = row
                start, finish = limits[-2:]
                if (start is None and kind[1] == "S" and state not in ("PENDIENTE", "BLOQUEADA", "CANCELADA") and rule != "PENDIENTE"
                        or finish is None and state != "CANCELADA" and rule == "COMPLETADA"):
                    raise RuntimeError("Historical dependencies require the current contract; preserve before reversal")
                try:
                    cursor.callproc("gm_validar_limites_dependencia", [kind, *limits])
                    while cursor.nextset():
                        pass
                except DatabaseError as failure:
                    raise RuntimeError("Existing dependencies require the current contract; preserve before reversal") from failure

        for name, (kind, previous) in OBJECTS.items():
            version = "0020" if forward else previous
            cursor.execute(f"DROP {kind} IF EXISTS {name}")
            cursor.execute((SQL / f"{name}.{version}.sql").read_text(encoding="utf8"))


def install(apps, schema_editor):
    replace_objects(schema_editor, True)


def uninstall(apps, schema_editor):
    replace_objects(schema_editor, False)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("proyectos", "0019_task_scheduling_dependencies")]
    operations = [migrations.RunPython(install, uninstall)]
