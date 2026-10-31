from django.db import transaction
from rest_framework.exceptions import ValidationError

from ..models import Worker, WorkerSpecialty


def worker_specialties(worker):
    return WorkerSpecialty.objects.filter(worker=worker).select_related("specialty")


@transaction.atomic
def upsert_worker_specialty(worker, specialty, *, level=None, increment=1):
    Worker.objects.select_for_update().get(pk=worker.pk)
    relation = (
        WorkerSpecialty.objects.select_for_update()
        .filter(worker=worker, specialty=specialty).first()
    )
    created = relation is None
    next_level = level if level is not None else (1 if created else relation.level + increment)
    if not 1 <= next_level <= 3:
        raise ValidationError({"level": "El nivel debe estar entre 1 y 3."})
    if created:
        relation = WorkerSpecialty.objects.create(worker=worker, specialty=specialty, level=next_level)
    else:
        relation.level = next_level
        relation.save(update_fields=["level"])
    return relation, created


@transaction.atomic
def delete_worker_specialty(worker, specialty):
    Worker.objects.select_for_update().get(pk=worker.pk)
    WorkerSpecialty.objects.filter(worker=worker, specialty=specialty).delete()


@transaction.atomic
def replace_worker_specialties(worker, specialties):
    Worker.objects.select_for_update().get(pk=worker.pk)
    WorkerSpecialty.objects.filter(worker=worker).delete()
    WorkerSpecialty.objects.bulk_create([
        WorkerSpecialty(worker=worker, specialty=item["specialty"], level=item.get("level", 1))
        for item in specialties
    ])
