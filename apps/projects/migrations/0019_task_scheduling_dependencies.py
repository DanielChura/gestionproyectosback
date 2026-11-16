import os
from pathlib import Path

from django.db import migrations, models


FIELDS = {
    "precision_calendario": "VARCHAR(4) NOT NULL DEFAULT 'day'",
    "hora_inicio_planificada": "TIME(6) NULL",
    "hora_fin_planificada": "TIME(6) NULL",
    "inicio_real_en": "DATETIME(6) NULL",
    "fin_real_en": "DATETIME(6) NULL",
}
OBJECTS = {
    "gm_validar_limites_dependencia": "PROCEDURE",
    "gm_validar_dependencias_programacion": "PROCEDURE",
    "gm_bi_programacion": "TRIGGER",
    "gm_bu_programacion": "TRIGGER",
    "gm_bi_dependencia_tipo": "TRIGGER",
    "gm_bu_dependencia_tipo": "TRIGGER",
    "gm_au_programacion_historial": "TRIGGER",
}


def maintained(connection):
    if connection.vendor == "mysql" and os.environ.get("TASK_SCHEDULING_MIGRATION_MAINTENANCE") != "1":
        raise RuntimeError("Stop task writes and enable TASK_SCHEDULING_MIGRATION_MAINTENANCE")


def install(apps, schema_editor):
    connection = schema_editor.connection
    maintained(connection)
    if connection.vendor != "mysql":
        raise RuntimeError("Scheduling DDL requires isolated or maintained MySQL; SQLite API tests use explicit model fixtures")
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        if not {"actividad", "dependencia_actividad"} <= tables:
            raise RuntimeError("Scheduling preflight requires the current task and dependency schema")
        columns = {item.name for item in connection.introspection.get_table_description(cursor, "actividad")}
        if columns & FIELDS.keys():
            raise RuntimeError("Scheduling columns already exist; preserve and review partial DDL before continuing")
        dependency_columns = {item.name for item in connection.introspection.get_table_description(cursor, "dependencia_actividad")}
        if "tipo" in dependency_columns:
            raise RuntimeError("Dependency type already exists; preserve and review")
        if connection.vendor == "mysql":
            cursor.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('actividad','dependencia_actividad')")
            if set(cursor.fetchall()) != {("actividad", "InnoDB"), ("dependencia_actividad", "InnoDB")}:
                raise RuntimeError("Scheduling requires InnoDB task and dependency tables")
            for name, kind in OBJECTS.items():
                table = "TRIGGERS" if kind == "TRIGGER" else "ROUTINES"
                prefix = "TRIGGER" if kind == "TRIGGER" else "ROUTINE"
                cursor.execute(f"SELECT COUNT(*) FROM information_schema.{table} WHERE {prefix}_SCHEMA=DATABASE() AND {prefix}_NAME=%s", [name])
                if cursor.fetchone()[0]:
                    raise RuntimeError("Scheduling object already exists; preserve and review")
            cursor.execute("SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='gm_bu_actividad_calendario'")
            row = cursor.fetchone()
            original = (Path(__file__).parent / "sql/gm_bu_actividad_calendario.0017.sql").read_text(encoding="utf8").split("FOR EACH ROW", 1)[1]
            if not row or " ".join(row[0].split()).lower() != " ".join(original.split()).lower():
                raise RuntimeError("Calendar trigger differs from reviewed 0017; preserve and review")
        for name, sql_type in FIELDS.items():
            cursor.execute(f"ALTER TABLE actividad ADD COLUMN {name} {sql_type}")
        cursor.execute("ALTER TABLE dependencia_actividad ADD COLUMN tipo VARCHAR(2) NULL CHECK (tipo IS NULL OR tipo IN ('FS','SS','FF','SF'))")
        cursor.execute("ALTER TABLE actividad ADD CONSTRAINT ck_actividad_precision CHECK (precision_calendario IN ('day','hour'))")
        if connection.vendor == "mysql":
            for name in OBJECTS:
                cursor.execute((Path(__file__).parent / f"sql/{name}.0019.sql").read_text(encoding="utf8"))


def uninstall(apps, schema_editor):
    connection = schema_editor.connection
    maintained(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM actividad WHERE precision_calendario<>'day' OR hora_inicio_planificada IS NOT NULL OR hora_fin_planificada IS NOT NULL OR inicio_real_en IS NOT NULL OR fin_real_en IS NOT NULL")
        used = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dependencia_actividad WHERE tipo IS NOT NULL")
        if used or cursor.fetchone()[0]:
            raise RuntimeError("Scheduling or typed dependencies are in use; reversal would lose information")
        if connection.vendor == "mysql":
            for name, kind in reversed(list(OBJECTS.items())):
                cursor.execute(f"DROP {kind} {name}")
            cursor.execute("ALTER TABLE actividad DROP CHECK ck_actividad_precision")
        cursor.execute("ALTER TABLE dependencia_actividad DROP COLUMN tipo")
        for name in reversed(FIELDS):
            cursor.execute(f"ALTER TABLE actividad DROP COLUMN {name}")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("proyectos", "0018_tema_remove_projecttaskstatus_color_and_more")]
    operations = [
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.AddField(model_name="task", name="calendar_precision", field=models.CharField(max_length=4, default="day", db_column="precision_calendario")),
            migrations.AddField(model_name="task", name="start_time", field=models.TimeField(null=True, db_column="hora_inicio_planificada")),
            migrations.AddField(model_name="task", name="due_time", field=models.TimeField(null=True, db_column="hora_fin_planificada")),
            migrations.AddField(model_name="task", name="actual_started_at", field=models.DateTimeField(null=True, db_column="inicio_real_en")),
            migrations.AddField(model_name="task", name="actual_finished_at", field=models.DateTimeField(null=True, db_column="fin_real_en")),
            migrations.AddField(model_name="taskdependency", name="dependency_type", field=models.CharField(max_length=2, null=True, db_column="tipo")),
        ]),
        migrations.RunPython(install, uninstall),
    ]
