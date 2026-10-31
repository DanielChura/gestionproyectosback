from datetime import timedelta
import hashlib
import json

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from ..models import TaskCalendarIntent


CALENDAR_FIELDS = {"start_date", "due_date"}
PRECONDITIONS = {"expected_calendar_revision", "calendar_intent_id"}
INTENT_LIFETIME = timedelta(days=7)


class CalendarConflict(APIException):
    status_code = 409

    def __init__(self, code, detail, **data):
        super().__init__({"code": code, "detail": detail, **data})


class CalendarIntentExpired(APIException):
    status_code = 410
    default_detail = {"code": "calendar_intent_expired", "detail": "La intención de calendario ha caducado; consulta el calendario antes de decidir una nueva edición."}


def validate_calendar_preconditions(data):
    if CALENDAR_FIELDS & data.keys():
        errors = {field: "Campo obligatorio al editar fechas." for field in PRECONDITIONS if field not in data}
        if errors:
            raise ValidationError(errors)
    elif PRECONDITIONS & data.keys():
        raise ValidationError({"calendar_intent_id": "La revisión y la intención solo se admiten al editar fechas."})


def payload_fingerprint(data):
    normalized = {key: value for key, value in data.items() if key != "calendar_intent_id"}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=lambda value: value.isoformat()).encode()).hexdigest()


def receipt(task, intent, replayed):
    task.calendar_receipt = {"intent_id": str(intent.key), "applied_revision": str(intent.applied_revision), "replayed": replayed}


def begin_calendar_edit(user, task, data):
    validate_calendar_preconditions(data)
    if not CALENDAR_FIELDS & data.keys():
        return None, False
    fingerprint = payload_fingerprint(data)
    scope = {"actor": user, "task": task, "operation": "calendar_patch", "key": data["calendar_intent_id"]}
    existing = TaskCalendarIntent.objects.select_for_update().filter(**scope).first()
    if existing is None:
        try:
            with transaction.atomic():
                existing = TaskCalendarIntent.objects.create(
                    **scope, payload_hash=fingerprint, expected_revision=data["expected_calendar_revision"],
                    expires_at=timezone.now() + INTENT_LIFETIME,
                )
            created = True
        except IntegrityError:
            existing = TaskCalendarIntent.objects.select_for_update().get(**scope)
            created = False
    else:
        created = False
    if not created:
        if existing.payload_hash != fingerprint:
            raise CalendarConflict("calendar_intent_reused", "La clave ya identifica otro contenido; no se ha aplicado la edición.")
        if existing.expires_at <= timezone.now() or existing.status == "expired":
            raise CalendarIntentExpired()
        if existing.status != "succeeded":
            raise CalendarConflict("calendar_intent_pending", "La intención todavía no tiene un éxito confirmado; consulta su resultado sin cambiar la clave.")
        receipt(task, existing, True)
        return existing, True
    if task.calendar_revision != data["expected_calendar_revision"]:
        raise CalendarConflict("calendar_revision_conflict", "El calendario cambió; consulta la versión actual antes de editar.",
                               calendar_revision=str(task.calendar_revision))
    return existing, False


def finish_calendar_edit(task, intent):
    intent.status = "succeeded"
    intent.applied_revision = task.calendar_revision
    intent.save(update_fields=["status", "applied_revision"])
    receipt(task, intent, False)

