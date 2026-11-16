from contextlib import contextmanager
from datetime import timedelta

from django.core import signing
from django.db import connection
from django.utils import timezone
from rest_framework.exceptions import ValidationError


SALT = "projects.dependency.undo"


def issue_undo(user, project_id, predecessor_id, successor_id, kind):
    expires = (timezone.now() + timedelta(seconds=10)).timestamp()
    token = signing.dumps({"actor": user.pk, "project": project_id, "source": predecessor_id,
                          "target": successor_id, "type": kind,
                          "expires": expires}, salt=SALT)
    return token, int(expires * 1000)


def read_undo(token, user, project_id, predecessor_id, successor_id):
    try:
        payload = signing.loads(token, salt=SALT)
    except signing.BadSignature:
        raise ValidationError({"undo_token": "La restauración no es válida."}) from None
    if (payload.get("actor"), payload.get("project"), payload.get("source"), payload.get("target")) != (
        user.pk, project_id, predecessor_id, successor_id
    ) or payload.get("expires", 0) <= timezone.now().timestamp():
        raise ValidationError({"undo_token": "El plazo para restaurar este vínculo terminó o pertenece a otra operación."})
    return payload["type"]


@contextmanager
def restoring_legacy(enabled):
    if connection.vendor != "mysql" or not enabled:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT @gm_dependency_restore")
        previous = cursor.fetchone()[0]
        cursor.execute("SET @gm_dependency_restore=1")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET @gm_dependency_restore=%s", [previous])
