from collections.abc import Mapping
from datetime import timedelta
import hmac
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.organization.models import Area
from apps.workers.models import Worker, WorkerArea
from apps.workers.services.areas import area_summary, authorized_areas

from ..mail import DeliveryError, send_link
from ..models import AuthToken, Invitation, Role, User, UserRole
from ..audit import record_auth_event
from ..security import consume_send_quota
from ..serializers import PasswordSerializer
from ..tokens import InvalidToken, consume_stored, decode_token, digest_token, inspect_stored, issue_stored
from .eligibility import account_eligible


class InvitationConflict(APIException):
    status_code = 409
    default_detail = "Hay un envío en curso. Intenta nuevamente en un momento"


class InvitationDeliveryError(APIException):
    status_code = 503
    default_detail = "La cuenta quedó pendiente. No se confirmó el envío; puedes reintentarlo desde Cuentas"


def email_digest(email):
    return digest_token(email.strip().lower())


def register_account(data, actor):
    try:
        with transaction.atomic():
            if not Role.objects.filter(pk=data["role"]).exists():
                raise ValidationError({"role": "El rol no está disponible"})
            selected = list(Area.objects.select_for_update().filter(active=True, pk__in=data["area_ids"]))
            if not data["all_areas"] and len(selected) != len(data["area_ids"]):
                raise ValidationError({"area_ids": "Hay áreas inexistentes o inactivas"})
            worker = Worker.objects.create(
                code=f"GM-{uuid4().hex[:26]}",
                first_names=data["first_names"],
                last_names=data["last_names"],
                email=data["email"],
                active=True,
                area_id=None if data["all_areas"] else data["area_ids"][0],
                all_areas=data["all_areas"],
            )
            WorkerArea.objects.bulk_create([WorkerArea(worker=worker, area_id=area_id) for area_id in data["area_ids"]])
            username = data["email"] if len(data["email"]) <= 80 else f"gm_{uuid4().hex}"
            user = User.objects.create(
                worker=worker, username=username, password=make_password(None), is_active=False
            )
            UserRole.objects.create(user=user, role_code=data["role"])
            Invitation.objects.create(user=user, created_by=actor, email_digest=email_digest(worker.email))
    except IntegrityError:
        if Worker.objects.filter(email__iexact=data["email"]).exists():
            raise ValidationError({"email": "El correo ya está registrado"}) from None
        raise
    return user


def deliver_invitation(user_id, actor):
    with transaction.atomic():
        user = User.objects.select_related("worker").select_for_update().filter(pk=user_id).first()
        if user is None or user.is_active or not user.worker.active or not user.worker.email or not user.global_role:
            raise ValidationError({"detail": "La cuenta pendiente no está disponible"})
        if not authorized_areas(user.worker).exists():
            raise ValidationError({"detail": "La cuenta no tiene áreas activas disponibles"})
        invitation, _ = Invitation.objects.get_or_create(
            user=user, defaults={"created_by": actor, "email_digest": email_digest(user.worker.email)}
        )
        now = timezone.now()
        if (
            invitation.delivery_status == "sending"
            and invitation.last_attempt_at
            and invitation.last_attempt_at > now - timedelta(seconds=60)
        ):
            raise InvitationConflict
        consume_send_quota("activation", user.pk)
        token = issue_stored(user, "activation", now + timedelta(hours=settings.AUTH_LINK_HOURS), revoke_previous=True)
        record = AuthToken.objects.get(digest=digest_token(token))
        invitation.token = record
        invitation.email_digest = email_digest(user.worker.email)
        invitation.delivery_status = "sending"
        invitation.last_attempt_at = now
        invitation.provider_message_id = ""
        invitation.save()
    try:
        message_id = send_link(user.worker.email, user.worker.first_names, "activation", token)
    except DeliveryError as error:
        Invitation.objects.filter(pk=invitation.pk, token=record).update(delivery_status=error.delivery_status)
        if error.delivery_status == "failed":
            AuthToken.objects.filter(pk=record.pk).update(consumed_at=timezone.now())
        record_auth_event(
            "invitation_failed",
            actor_id=getattr(actor, "pk", None),
            account_id=user.pk,
            detail=error.delivery_status,
        )
        raise InvitationDeliveryError(
            {"detail": InvitationDeliveryError.default_detail, "account_id": user.pk}
        ) from None
    Invitation.objects.filter(pk=invitation.pk, token=record).update(
        delivery_status="accepted", provider_message_id=message_id
    )
    record_auth_event(
        "invitation_sent",
        actor_id=getattr(actor, "pk", None),
        account_id=user.pk,
    )
    return {"detail": "Invitación aceptada por el proveedor de correo", "account_id": user.pk}


def link_account(token, purpose):
    record = inspect_stored(token, purpose)
    user = record.user
    if not user.worker.active or not user.worker.email or not user.global_role:
        raise InvalidToken
    if purpose == "activation":
        invitation = Invitation.objects.filter(user=user, token=record).first()
        if (
            user.is_active
            or invitation is None
            or not hmac.compare_digest(invitation.email_digest, email_digest(user.worker.email))
        ):
            raise InvalidToken
        if not authorized_areas(user.worker).exists():
            raise InvalidToken
    elif not account_eligible(user):
        raise InvalidToken
    return user


def link_details(token, purpose):
    user = link_account(token, purpose)
    return {
        "first_names": user.worker.first_names,
        "last_names": user.worker.last_names,
        "email": user.worker.email,
        "role": user.global_role,
        **area_summary(user.worker),
    }


def validate_link_password(data, purpose):
    if not isinstance(data, Mapping):
        raise ValidationError({"detail": "Se requiere un objeto de datos"})
    user = link_account(data.get("token", ""), purpose)
    serializer = PasswordSerializer(data=data, context={"user": user})
    serializer.is_valid(raise_exception=True)
    return user, serializer.validated_data


def complete_password(data, purpose):
    if not isinstance(data, Mapping):
        raise ValidationError({"detail": "Se requiere un objeto de datos"})
    claims = decode_token(data.get("token", ""), purpose)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=int(claims["sub"]))
        user, validated = validate_link_password(data, purpose)
        consume_stored(validated["token"], purpose)
        user.set_password(validated["password"])
        fields = ["password"]
        if purpose == "activation":
            user.is_active = True
            fields.append("is_active")
        user.save(update_fields=fields)
        AuthToken.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
