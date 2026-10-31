from ..models import Specialty


def specialty_options():
    return Specialty.objects.order_by("name", "id").values("id", "name")
