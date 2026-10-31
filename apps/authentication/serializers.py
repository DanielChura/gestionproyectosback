from collections.abc import Mapping

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as PasswordError
from rest_framework import serializers

from apps.pagination import ListQuerySerializer
from apps.organization.models import Area
from apps.workers.models import Worker

from .models import FINAL_ROLES


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({"detail": "Se requiere un objeto de datos"})
        extra = set(data) - set(self.fields)
        if extra:
            raise serializers.ValidationError({"detail": "La solicitud contiene campos no permitidos"})
        return super().to_internal_value(data)


class EmailSerializer(StrictSerializer):
    email = serializers.EmailField(max_length=254)

    def validate_email(self, value):
        return value.strip().lower()


class LoginSerializer(EmailSerializer):
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)


class InvitationSerializer(StrictSerializer):
    first_names = serializers.CharField(max_length=100)
    last_names = serializers.CharField(max_length=120)
    email = serializers.EmailField(max_length=254)
    role = serializers.ChoiceField(choices=FINAL_ROLES)
    all_areas = serializers.BooleanField(default=False)
    area_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), default=list)

    def validate_email(self, value):
        email = value.strip().lower()
        if Worker.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("El correo ya está registrado")
        return email

    def validate(self, attrs):
        ids = attrs["area_ids"]
        if attrs["all_areas"]:
            if ids:
                raise serializers.ValidationError({"area_ids": "Todas no admite selecciones individuales"})
            if not Area.objects.filter(active=True).exists():
                raise serializers.ValidationError({"area_ids": "No hay áreas activas disponibles"})
        else:
            if not ids or len(ids) != len(set(ids)):
                raise serializers.ValidationError({"area_ids": "Selecciona al menos un área, sin duplicados"})
            if Area.objects.filter(pk__in=ids, active=True).count() != len(ids):
                raise serializers.ValidationError({"area_ids": "Hay áreas inexistentes o inactivas"})
        return attrs


class TokenSerializer(StrictSerializer):
    token = serializers.CharField(max_length=2048, trim_whitespace=False)


class PasswordSerializer(TokenSerializer):
    password = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)
    password_confirmation = serializers.CharField(max_length=1024, trim_whitespace=False, write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirmation"]:
            raise serializers.ValidationError({"password_confirmation": "Las contraseñas no coinciden"})
        try:
            validate_password(attrs["password"], user=self.context["user"])
        except PasswordError as error:
            raise serializers.ValidationError({"password": error.messages}) from None
        return attrs


class AccountListQuerySerializer(ListQuerySerializer):
    active = serializers.BooleanField(required=False)
