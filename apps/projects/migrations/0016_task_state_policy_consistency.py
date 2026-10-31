from importlib import import_module
from pathlib import Path
import os
import re

from django.db import DatabaseError, migrations


previous = import_module("apps.projects.migrations.0015_task_state_without_assignee")
direct = previous.previous
baseline = direct.baseline
helpers = baseline.helpers
KEY = "task_state_policy_consistency_0016"
NAMES = (direct.NAME, direct.MODE)
REASON = """ IF v_req_motivo=1 AND (p_motivo IS NULL OR CHAR_LENGTH(TRIM(p_motivo))=0) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Debe indicar el motivo del cambio.';
 END IF;
"""
ASSIGNEE = """  IF v_responsable IS NULL OR NOT EXISTS(SELECT 1 FROM proyecto_miembro pm JOIN trabajador t ON t.id=pm.trabajador_id
   WHERE pm.proyecto_id=p_proyecto AND pm.trabajador_id=v_responsable AND pm.activo=1 AND t.activo=1) THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='La actividad debe tener un responsable activo del proyecto.';
  END IF;
"""


def source(name):
    if name == direct.NAME:
        return (Path(__file__).parent / "sql" / "sp_aplicar_estado_actividad.0016.sql").read_text(encoding="utf-8")
    return f"""CREATE PROCEDURE `{direct.MODE}`(IN p_proyecto BIGINT UNSIGNED,
 IN p_actividad BIGINT UNSIGNED,IN p_destino VARCHAR(20),IN p_motivo VARCHAR(500),IN p_movimiento BOOLEAN)
SQL SECURITY INVOKER
BEGIN
 CALL {direct.NAME}(p_proyecto,p_actividad,p_destino,p_motivo);
END"""


def historical(name, reason=False, assignee=False):
    if name == direct.NAME:
        return direct.wrapper()
    sql = direct.source()
    if reason:
        marker = " IF v_regla_destino NOT IN ('COMPLETADA','CANCELADA') THEN\n"
        if sql.count(marker) != 1:
            raise RuntimeError("Historical reason policy requires review")
        sql = sql.replace(marker, REASON + marker, 1)
    if assignee:
        marker = " IF v_regla_destino IN ('EN_CURSO','EN_REVISION','COMPLETADA') THEN\n"
        if sql.count(marker) != 1:
            raise RuntimeError("Historical assignee policy requires review")
        sql = sql.replace(marker, marker + ASSIGNEE, 1)
    return sql


QUOTED_IDENTIFIER = r'(?:`(?:``|[^`])*`|"(?:\"\"|[^\"])*")'
PROCEDURE_HEADER = re.compile(
    rf"\ACREATE\s+(?:DEFINER\s*=\s*(?P<user>{QUOTED_IDENTIFIER})\s*@\s*"
    rf"(?P<host>{QUOTED_IDENTIFIER})\s+)?PROCEDURE\s+"
    rf"(?P<name>{QUOTED_IDENTIFIER}|[a-zA-Z_][a-zA-Z_0-9]*)(?P<tail>\s*\(.*)\Z",
    re.IGNORECASE | re.DOTALL,
)


def definition_header(definition, name):
    match = PROCEDURE_HEADER.fullmatch(definition["Create Procedure"].strip())
    if match is None:
        raise RuntimeError(f"Task state procedure header requires review: {name}")
    tokens = [match.group(key) for key in ("user", "host", "name") if match.group(key)]
    if any(token.startswith('"') for token in tokens):
        if "ANSI_QUOTES" not in definition.get("sql_mode", "").upper().split(","):
            raise RuntimeError(f"Task state identifier quoting requires ANSI_QUOTES: {name}")
    identifier = match["name"]
    if identifier[0] in ('"', "`"):
        identifier = identifier[1:-1].replace(identifier[0] * 2, identifier[0])
    if identifier != name:
        raise RuntimeError(f"Task state procedure name differs: {name}")
    return match


def normalized_definition(definition, name):
    header = definition_header(definition, name)
    return baseline.normalized(f"CREATE PROCEDURE `{name}`{header['tail']}")


def with_definer(sql, definition, name):
    header = definition_header(definition, name)
    if header["user"] is None:
        return sql
    return sql.replace("CREATE PROCEDURE", f"CREATE DEFINER={header['user']}@{header['host']} PROCEDURE", 1)


def reviewed(definition, name, installed=False):
    candidates = [source(name)] if installed else [historical(name), historical(name, True), historical(name, True, True), source(name)]
    if not installed and name == direct.NAME:
        candidates.append(baseline.source(name))
    if normalized_definition(definition, name) not in {
        normalized_definition({"Create Procedure": sql}, name) for sql in candidates
    }:
        raise RuntimeError(f"Task state policy differs from reviewed definitions: {name}; preserve and review")


def maintained(connection):
    if connection.vendor == "mysql" and os.environ.get("TASK_STATE_POLICY_MAINTENANCE") != "1":
        raise RuntimeError("Stop task writes and enable TASK_STATE_POLICY_MAINTENANCE")


def install(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    maintained(connection)
    with connection.cursor() as cursor:
        originals = {name: helpers.read_definition(cursor, "PROCEDURE", name) for name in NAMES}
        for name, definition in originals.items():
            reviewed(definition, name)
        backups = apps.get_model("autenticacion", "MigrationBackup").objects.using(connection.alias)
        saved, created = backups.get_or_create(key=KEY, defaults={"payload": {"procedures": originals}})
        if set(saved.payload.get("procedures", {})) != set(NAMES):
            raise RuntimeError("Task state policy backup requires review")
        for name, definition in saved.payload["procedures"].items():
            reviewed(definition, name)
            if not created and normalized_definition(originals[name], name) not in (
                normalized_definition(definition, name),
                normalized_definition({"Create Procedure": source(name)}, name),
            ):
                raise RuntimeError("Task state policy backup differs; preserve and review")
        try:
            for name in NAMES:
                definition = originals[name]
                helpers.recreate(cursor, name, with_definer(source(name), definition, name), definition)
                reviewed(helpers.read_definition(cursor, "PROCEDURE", name), name, installed=True)
        except (DatabaseError, RuntimeError):
            for name, definition in reversed(list(originals.items())):
                helpers.recreate(cursor, name, definition["Create Procedure"], definition)
            raise


def restore(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return
    maintained(connection)
    with connection.cursor() as cursor:
        current = {name: helpers.read_definition(cursor, "PROCEDURE", name) for name in NAMES}
        for name, definition in current.items():
            reviewed(definition, name, installed=True)
        saved = apps.get_model("autenticacion", "MigrationBackup").objects.using(connection.alias).get(key=KEY)
        originals = saved.payload.get("procedures", {})
        if set(originals) != set(NAMES):
            raise RuntimeError("Task state policy backup requires review")
        for name, definition in originals.items():
            reviewed(definition, name)
        try:
            for name, definition in reversed(list(originals.items())):
                helpers.recreate(cursor, name, definition["Create Procedure"], definition)
        except (DatabaseError, RuntimeError):
            for name, definition in reversed(list(current.items())):
                helpers.recreate(cursor, name, definition["Create Procedure"], definition)
            raise
    saved.delete()


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("proyectos", "0015_task_state_without_assignee")]
    operations = [migrations.RunPython(install, restore)]
