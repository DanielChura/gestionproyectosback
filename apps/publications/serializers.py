from decimal import Decimal

from rest_framework import serializers

from apps.authentication.serializers import StrictSerializer

from .models import PublicationState


class PublicationRequirementSerializer(StrictSerializer):
    specialty_id = serializers.IntegerField(min_value=1)
    minimum_level = serializers.IntegerField(min_value=1, max_value=3)


def _validate_unique_specialties(requirements):
    ids = [item["specialty_id"] for item in requirements]
    if len(ids) != len(set(ids)):
        raise serializers.ValidationError({"requirements": "No se permiten especialidades duplicadas"})


class PublicationCreateSerializer(StrictSerializer):
    project_id = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=180)
    description = serializers.CharField(max_length=10000)
    vacancies = serializers.IntegerField(min_value=1, max_value=65535)
    weekly_hours = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("168"),
    )
    opening_date = serializers.DateField()
    closing_date = serializers.DateField()
    state = serializers.ChoiceField(choices=PublicationState.choices, required=False)
    requirements = PublicationRequirementSerializer(many=True, default=list, max_length=50)

    def validate(self, attrs):
        _validate_unique_specialties(attrs["requirements"])
        return attrs


class PublicationUpdateSerializer(StrictSerializer):
    title = serializers.CharField(max_length=180, required=False)
    description = serializers.CharField(max_length=10000, required=False)
    vacancies = serializers.IntegerField(min_value=1, max_value=65535, required=False)
    weekly_hours = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("168"),
        required=False,
    )
    opening_date = serializers.DateField(required=False)
    closing_date = serializers.DateField(required=False)
    state = serializers.ChoiceField(choices=PublicationState.choices, required=False)
    requirements = PublicationRequirementSerializer(many=True, required=False, max_length=50)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError({"detail": "Indica los datos que quieres modificar"})
        if "requirements" in attrs:
            _validate_unique_specialties(attrs["requirements"])
        return attrs
