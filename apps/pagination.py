from django.core import signing
from django.db.models import Max, Q
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from rest_framework.exceptions import ValidationError


class ListQuerySerializer(serializers.Serializer):
    search = serializers.CharField(max_length=100, required=False, default="", allow_blank=True)
    page_size = serializers.IntegerField(min_value=1, max_value=50, required=False, default=25)
    cursor = serializers.CharField(max_length=4096, required=False, default="", allow_blank=True)


def cursor_page(queryset, *, query, user, resource, serialize, oldest=False):
    filters = {key: value for key, value in query.items() if key not in ("cursor", "page_size")}
    size = query["page_size"]
    binding = {"resource": resource, "user": user.pk, "role": user.global_role, "filters": filters, "size": size}
    salt = "gm.authorized-list.v1"
    if query["cursor"]:
        try:
            position = signing.loads(query["cursor"], salt=salt)
            if position["binding"] != binding or type(position["upper"]) is not int:
                raise ValueError
            after = position["after"]
            if after is not None and (type(after["id"]) is not int or (oldest and not parse_datetime(after["time"]))):
                raise ValueError
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            raise ValidationError({"cursor": "La página no corresponde a estos filtros. Actualiza el listado."}) from None
    else:
        position = {"binding": binding, "upper": queryset.aggregate(value=Max("pk"))["value"] or 0, "after": None}
    queryset = queryset.filter(pk__lte=position["upper"])
    total = queryset.count()
    after = position["after"]
    if after is not None:
        if oldest:
            queryset = queryset.filter(Q(requested_at__gt=parse_datetime(after["time"])) |
                                      Q(requested_at=parse_datetime(after["time"]), pk__gt=after["id"]))
        else:
            queryset = queryset.filter(pk__lt=after["id"])
    queryset = queryset.order_by("requested_at", "pk") if oldest else queryset.order_by("-pk")
    rows = list(queryset[:size + 1])
    more = len(rows) > size
    rows = rows[:size]
    next_cursor = None
    if more:
        marker = {"id": rows[-1].pk}
        if oldest:
            marker["time"] = rows[-1].requested_at.isoformat()
        next_cursor = signing.dumps({**position, "after": marker}, salt=salt, compress=True)
    return {
        "results": serialize(rows), "total": total, "cursor": signing.dumps(position, salt=salt, compress=True),
        "next_cursor": next_cursor,
    }
