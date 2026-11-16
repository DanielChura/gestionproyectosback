from django.utils import timezone
from rest_framework import serializers

from .models import Team, TeamMember


TEAM_MINIMUM_MESSAGE = (
    "Un equipo activo necesita al menos 2 integrantes activos. "
    "Crea el equipo como inactivo, agrega 2 integrantes y luego actívalo."
)


class TeamSerializer(serializers.ModelSerializer):
    area_name = serializers.CharField(source="area.name", read_only=True)
    active_members = serializers.IntegerField(read_only=True)

    class Meta:
        model = Team
        fields = ["id", "area", "area_name", "name", "active", "active_members"]
        read_only_fields = ["id", "area_name", "active_members"]

    def validate_name(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("El nombre es obligatorio")
        if len(cleaned) > 120:
            raise serializers.ValidationError("El nombre no puede superar 120 caracteres")
        return cleaned

    def validate(self, attrs):
        active = attrs.get("active", self.instance.active if self.instance else False)
        if not active:
            return attrs
        if self.instance is None:
            raise serializers.ValidationError({"active": TEAM_MINIMUM_MESSAGE})
        count = TeamMember.objects.filter(
            team=self.instance, left_date__isnull=True
        ).count()
        if count < 2:
            raise serializers.ValidationError(
                {
                    "active": (
                        "No se puede activar el equipo porque tiene menos de 2 integrantes activos. "
                        "Agrega integrantes y vuelve a intentarlo."
                    )
                }
            )
        return attrs


class TeamMemberSerializer(serializers.ModelSerializer):
    worker_name = serializers.SerializerMethodField()

    class Meta:
        model = TeamMember
        fields = ["id", "worker", "worker_name", "joined_date", "left_date"]
        read_only_fields = ["id", "worker_name"]

    def get_worker_name(self, obj):
        worker = getattr(obj, "worker", None)
        if worker is None:
            return ""
        return f"{worker.first_names} {worker.last_names}".strip()


class TeamMemberCreateSerializer(serializers.Serializer):
    worker_id = serializers.IntegerField(min_value=1)
    joined_date = serializers.DateField(required=False, default=timezone.localdate)

    def validate_joined_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("La fecha de ingreso no puede ser futura")
        return value
