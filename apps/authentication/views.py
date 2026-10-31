from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.pagination import ListQuerySerializer
from apps.workers.services.areas import area_summary

from .mail import DeliveryError, send_link
from .models import AuthToken, RecoveryRequest, User
from .security import IsAdministrator, check_throttle, clear_throttle, record_failure, role_annotation
from .serializers import (
    AccountListQuerySerializer,
    EmailSerializer,
    InvitationSerializer,
    LoginSerializer,
    PasswordSerializer,
    TokenSerializer,
)
from .services.accounts import (
    complete_password,
    deliver_invitation,
    link_details,
    register_account,
    validate_link_password,
)
from .services.eligibility import account_eligible
from .services.listings import account_page, recovery_page
from .tokens import InvalidToken, consume_stored, decode_token, issue_access, issue_stored, next_monday_lima


def profile(user):
    return {
        "id": user.pk,
        "name": f"{user.worker.first_names} {user.worker.last_names}".strip(),
        "email": user.worker.email,
        "role": user.global_role,
        "permissions": {
            "manage_accounts": user.global_role == "ADMINISTRADOR",
            "manage_projects": user.global_role == "ADMINISTRADOR",
            "supervise_projects": user.global_role == "ADMINISTRADOR",
        },
        **area_summary(user.worker),
    }


def eligible(user):
    return account_eligible(user)


def cookie_response(payload, refresh_token=None, expires_at=None, clear=False, status_code=200):
    response = JsonResponse(payload, status=status_code)
    if clear:
        response.delete_cookie(settings.AUTH_REFRESH_COOKIE, path="/api/auth/", samesite=settings.AUTH_COOKIE_SAMESITE)
    if refresh_token:
        response.set_cookie(
            settings.AUTH_REFRESH_COOKIE,
            refresh_token,
            expires=expires_at,
            path="/api/auth/",
            httponly=True,
            secure=settings.AUTH_COOKIE_SECURE,
            samesite=settings.AUTH_COOKIE_SAMESITE,
        )
    response["Cache-Control"] = "no-store"
    return response


@ensure_csrf_cookie
def csrf_view(request):
    if request.method != "GET":
        return JsonResponse({"detail": "Método no permitido"}, status=405)
    return JsonResponse({"ok": True, "csrf_token": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = LoginSerializer

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]
        check_throttle(request, email, "login")
        with transaction.atomic():
            try:
                user = (
                    User.objects.select_related("worker")
                    .annotate(_role_code=role_annotation())
                    .select_for_update()
                    .get(worker__email__iexact=email)
                )
            except (User.DoesNotExist, User.MultipleObjectsReturned):
                user = None
            if not user or not eligible(user) or not user.check_password(password):
                record_failure(request, email, "login")
                return Response({"detail": "Credenciales no válidas"}, status=status.HTTP_401_UNAUTHORIZED)
            clear_throttle(request, email, "login")
            expires_at = next_monday_lima()
            AuthToken.objects.filter(user=user, purpose__in=["access", "refresh"], consumed_at__isnull=True).update(
                consumed_at=timezone.now()
            )
            refresh = issue_stored(user, "refresh", expires_at)
            payload = {"access": issue_access(user, expires_at), "user": profile(user)}
        return cookie_response(payload, refresh, expires_at)


@csrf_protect
def refresh_view(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Método no permitido"}, status=405)
    token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE, "")
    try:
        claims = decode_token(token, "refresh")
        with transaction.atomic():
            user = (
                User.objects.select_related("worker")
                .annotate(_role_code=role_annotation())
                .select_for_update()
                .get(pk=int(claims["sub"]))
            )
            _, expires_at = consume_stored(token, "refresh", user=user)
            if not eligible(user):
                raise InvalidToken
            refresh = issue_stored(user, "refresh", expires_at)
            payload = {"access": issue_access(user, expires_at), "user": profile(user)}
    except (InvalidToken, User.DoesNotExist):
        return cookie_response({"detail": "Sesión expirada"}, clear=True, status_code=401)
    return cookie_response(payload, refresh, expires_at)


@csrf_protect
def logout_view(request):
    if request.method != "POST":
        return JsonResponse({"detail": "Método no permitido"}, status=405)
    token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE, "")
    try:
        user, _ = consume_stored(token, "refresh")
        AuthToken.objects.filter(user=user, purpose="access", consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
    except InvalidToken:
        pass
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        try:
            consume_stored(authorization[7:], "access")
        except InvalidToken:
            pass
    return cookie_response({"ok": True}, clear=True)


class MeView(APIView):
    def get(self, request):
        return Response(profile(request.user))


class RecoveryRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = EmailSerializer

    def post(self, request):
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        check_throttle(request, email, "recovery")
        record_failure(request, email, "recovery")
        with transaction.atomic():
            user = (
                User.objects.select_related("worker", "worker__area")
                .select_for_update()
                .filter(worker__email__iexact=email)
                .first()
            )
            if (
                user
                and eligible(user)
                and not RecoveryRequest.objects.filter(user=user, resolved_at__isnull=True).exists()
            ):
                RecoveryRequest.objects.create(user=user)
        return Response({"detail": "Si la cuenta existe, la solicitud será revisada por un administrador"})


class LinkPreviewView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = TokenSerializer

    def post(self, request, purpose):
        serializer = TokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(link_details(serializer.validated_data["token"], purpose))
        except (InvalidToken, User.DoesNotExist):
            return Response({"detail": "Enlace inválido o vencido"}, status=400)


class LinkPasswordValidationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = PasswordSerializer

    def post(self, request, purpose):
        try:
            validate_link_password(request.data, purpose)
        except (InvalidToken, User.DoesNotExist):
            return Response({"detail": "Enlace inválido o vencido"}, status=400)
        return Response({"valid": True})


class PasswordCompletionView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = PasswordSerializer
    purpose = "activation"

    def post(self, request):
        try:
            complete_password(request.data, self.purpose)
        except (InvalidToken, User.DoesNotExist):
            return Response({"detail": "Enlace inválido o vencido"}, status=400)
        message = (
            "Cuenta activada. Ya puedes iniciar sesión"
            if self.purpose == "activation"
            else "Contraseña actualizada. Inicia sesión nuevamente"
        )
        return Response({"detail": message})


class ActivationView(PasswordCompletionView):
    purpose = "activation"


class ResetView(PasswordCompletionView):
    purpose = "reset"


class AdminAccountsView(APIView):
    permission_classes = [IsAdministrator]

    def get(self, request):
        serializer = AccountListQuerySerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        return Response(account_page(request.user, serializer.validated_data))


class AdminInviteView(APIView):
    permission_classes = [IsAdministrator]
    serializer_class = InvitationSerializer

    def post(self, request):
        serializer = InvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = register_account(serializer.validated_data, request.user)
        return Response(deliver_invitation(user.pk, request.user), status=201)


class AdminResendInvitationView(APIView):
    permission_classes = [IsAdministrator]

    def post(self, request, account_id):
        if request.data:
            raise ValidationError({"detail": "El reenvío no permite modificar la cuenta"})
        return Response(deliver_invitation(account_id, request.user))


class AdminRecoveryRequestsView(APIView):
    permission_classes = [IsAdministrator]

    def get(self, request):
        serializer = ListQuerySerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        return Response(recovery_page(request.user, serializer.validated_data))


class AdminIssueResetView(APIView):
    permission_classes = [IsAdministrator]

    def post(self, request, request_id):
        with transaction.atomic():
            entry = (
                RecoveryRequest.objects.select_related("user", "user__worker", "user__worker__area")
                .select_for_update()
                .filter(pk=request_id, resolved_at__isnull=True)
                .first()
            )
            if not entry or not eligible(entry.user):
                return Response({"detail": "Solicitud no disponible"}, status=404)
            expires_at = timezone.now() + timedelta(minutes=settings.AUTH_RESET_MINUTES)
            token = issue_stored(entry.user, "reset", expires_at, revoke_previous=True)
            try:
                send_link(entry.user.worker.email, entry.user.worker.first_names, "reset", token)
            except DeliveryError:
                raise ValidationError({"detail": "No se pudo enviar el enlace"}) from None
            entry.resolved_at = timezone.now()
            entry.resolved_by = request.user
            entry.save(update_fields=["resolved_at", "resolved_by"])
        return Response({"detail": "Enlace de restablecimiento enviado"})
