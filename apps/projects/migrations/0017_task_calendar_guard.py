import os
from pathlib import Path

from django.db import DatabaseError, migrations, models
from django.utils import timezone


TRIGGER = "gm_bu_actividad_calendario"


def maintained(connection):
    if connection.vendor == "mysql" and os.environ.get("TASK_CALENDAR_MIGRATION_MAINTENANCE") != "1":
        raise RuntimeError("Stop task writes and enable TASK_CALENDAR_MIGRATION_MAINTENANCE")


def trigger_sql():
    return (Path(__file__).parent / "sql/gm_bu_actividad_calendario.0017.sql").read_text(encoding="utf-8")


def preflight(connection):
    maintained(connection)
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        if "actividad" not in tables or "actividad_calendario_intento" in tables:
            raise RuntimeError("Expected previous activity schema and no calendar intent table; preserve and review")
        columns = {column.name: column for column in connection.introspection.get_table_description(cursor, "actividad")}
        if "calendario_revision" in columns:
            raise RuntimeError("Calendar revision already exists; preserve and review")
        if not {"fecha_inicio", "fecha_fin"} <= columns.keys():
            raise RuntimeError("Activity calendar columns are missing")
        if connection.vendor == "mysql":
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='actividad'")
            if cursor.fetchone()[0] != "InnoDB":
                raise RuntimeError("Activity must use InnoDB")
            cursor.execute("SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='actividad' AND COLUMN_NAME IN ('fecha_inicio','fecha_fin')")
            if set(cursor.fetchall()) != {("fecha_inicio", "date", "NO"), ("fecha_fin", "date", "YES")}:
                raise RuntimeError("Expected DATE start and nullable DATE deadline")
            cursor.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s", [TRIGGER])
            if cursor.fetchone()[0]:
                raise RuntimeError("Calendar trigger already exists; preserve and review")


def install(apps, schema_editor):
    connection = schema_editor.connection
    preflight(connection)
    intent = apps.get_model("proyectos", "TaskCalendarIntent")
    table_created = column_created = False
    try:
        schema_editor.create_model(intent)
        table_created = True
        with connection.cursor() as cursor:
            kind = "BIGINT UNSIGNED" if connection.vendor == "mysql" else "BIGINT"
            cursor.execute(f"ALTER TABLE actividad ADD COLUMN calendario_revision {kind} NOT NULL DEFAULT 1 CHECK (calendario_revision BETWEEN 1 AND 9223372036854775807)")
            column_created = True
            if connection.vendor == "mysql":
                cursor.execute(trigger_sql())
    except (DatabaseError, RuntimeError):
        with connection.cursor() as cursor:
            if connection.vendor == "mysql":
                cursor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER}")
            if column_created:
                cursor.execute("ALTER TABLE actividad DROP COLUMN calendario_revision")
        if table_created:
            schema_editor.delete_model(intent)
        raise


def uninstall(apps, schema_editor):
    connection = schema_editor.connection
    maintained(connection)
    intent = apps.get_model("proyectos", "TaskCalendarIntent")
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM actividad WHERE calendario_revision<>1")
        if cursor.fetchone()[0] or intent.objects.using(connection.alias).exists():
            raise RuntimeError("Calendar revisions or intentions are in use; reversal would lose concurrency guarantees")
        if connection.vendor == "mysql":
            cursor.execute("SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s", [TRIGGER])
            row = cursor.fetchone()
            expected = trigger_sql().split("FOR EACH ROW", 1)[1]
            if not row or " ".join(row[0].split()).lower() != " ".join(expected.split()).lower():
                raise RuntimeError("Calendar trigger changed; preserve and review")
            cursor.execute(f"DROP TRIGGER {TRIGGER}")
        cursor.execute("ALTER TABLE actividad DROP COLUMN calendario_revision")
    schema_editor.delete_model(intent)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("proyectos", "0016_task_state_policy_consistency")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(model_name="task", name="calendar_revision", field=models.PositiveBigIntegerField(default=1, db_column="calendario_revision")),
                migrations.CreateModel(
                    name="TaskCalendarIntent",
                    fields=[
                        ("id", models.BigAutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                        ("actor", models.ForeignKey("autenticacion.user", on_delete=models.DO_NOTHING, db_constraint=False)),
                        ("task", models.ForeignKey("proyectos.task", on_delete=models.DO_NOTHING, db_constraint=False)),
                        ("operation", models.CharField(max_length=32, default="calendar_patch")),
                        ("key", models.UUIDField()),
                        ("payload_hash", models.CharField(max_length=64)),
                        ("expected_revision", models.PositiveBigIntegerField()),
                        ("applied_revision", models.PositiveBigIntegerField(null=True)),
                        ("status", models.CharField(max_length=12, default="pending")),
                        ("created_at", models.DateTimeField(default=timezone.now)),
                        ("expires_at", models.DateTimeField()),
                    ],
                    options={"db_table": "actividad_calendario_intento",
                             "constraints": [models.UniqueConstraint(fields=["actor", "task", "operation", "key"], name="uq_calendario_intento")],
                             "indexes": [models.Index(fields=["expires_at", "status"], name="ix_calendario_caducidad")]},
                ),
            ],
        ),
        migrations.RunPython(install, uninstall),
    ]

