from uuid import uuid4

from django.db import transaction
from rest_framework import serializers

from apps.organization.models import Specialty
from .models import Worker, WorkerSpecialty
from .services.specialties import replace_worker_specialties


class WorkerSpecialtySerializer(serializers.ModelSerializer):
    id = serializers.PrimaryKeyRelatedField(source="specialty", queryset=Specialty.objects.all())
    level = serializers.IntegerField(min_value=1, max_value=3, required=False, default=1)
    name = serializers.CharField(source="specialty.name", read_only=True)

    class Meta:
        model = WorkerSpecialty
        fields = ["id", "name", "level"]


class WorkerSpecialtyTargetSerializer(serializers.Serializer):
    specialty_id = serializers.IntegerField(min_value=1, required=False)
    id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        identifier = (
            self.context.get("specialty_id")
            or attrs.pop("specialty_id", None) or attrs.pop("id", None)
        )
        if identifier is None:
            raise serializers.ValidationError({"specialty_id": "Selecciona una especialidad."})
        field = serializers.PrimaryKeyRelatedField(queryset=Specialty.objects.all())
        try:
            specialty = field.run_validation(identifier)
        except serializers.ValidationError as failure:
            raise serializers.ValidationError({"specialty_id": failure.detail}) from None
        attrs.pop("specialty_id", None)
        attrs.pop("id", None)
        attrs["specialty"] = specialty
        return attrs


class WorkerSpecialtyWriteSerializer(WorkerSpecialtyTargetSerializer):
    level = serializers.IntegerField(min_value=1, max_value=3, required=False, allow_null=True)
    increment = serializers.IntegerField(required=False, default=1)


class WorkerSerializer(serializers.ModelSerializer):
    specialties = WorkerSpecialtySerializer(many=True, required=False, source="worker_specialties")

    class Meta:
        model = Worker
        fields = [
            "id",
            "area",
            "code",
            "first_names",
            "last_names",
            "email",
            "weekly_hours",
            "active",
            "all_areas",
            "specialties",
        ]
        read_only_fields = ["id"]
        extra_kwargs = {"code": {"required": False}}

    def create(self, validated_data):
        validated_data.setdefault("code", f"W-{uuid4().hex[:8].upper()}")
        specialties_data = validated_data.pop("worker_specialties", [])
        with transaction.atomic():
            worker = Worker.objects.create(**validated_data)
            replace_worker_specialties(worker, specialties_data)
        return worker

    def update(self, instance, validated_data):
        specialties_data = validated_data.pop("worker_specialties", None)
        with transaction.atomic():
            instance = Worker.objects.select_for_update().get(pk=instance.pk)
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if specialties_data is not None:
                replace_worker_specialties(instance, specialties_data)
        return instance

    def validate_specialties(self, values):
        identifiers = [item["specialty"].pk for item in values]
        if len(identifiers) != len(set(identifiers)):
            raise serializers.ValidationError("No repitas una especialidad.")
        return values
