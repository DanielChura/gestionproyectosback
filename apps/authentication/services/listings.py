from django.db.models import Q

from apps.pagination import cursor_page
from apps.workers.services.areas import area_summaries
from ..models import RecoveryRequest, User


def search_people(queryset, search, prefix):
    for word in search.split():
        queryset = queryset.filter(
            Q(**{prefix + "first_names__icontains": word})
            | Q(**{prefix + "last_names__icontains": word})
            | Q(**{prefix + "email__icontains": word})
        )
    return queryset


def account_snapshots(accounts):
    areas = area_summaries(user.worker for user in accounts)
    return [
        {
            "id": user.pk, "name": f"{user.worker.first_names} {user.worker.last_names}",
            "email": user.worker.email, "role": user.global_role, "active": user.is_active,
            "delivery_status": user.invitation.delivery_status if hasattr(user, "invitation") else "pending",
            **areas[user.worker_id],
        }
        for user in accounts
    ]


def account_page(user, query):
    accounts = User.objects.select_related("worker", "invitation").prefetch_related("userrole_set")
    accounts = search_people(accounts, query["search"], "worker__")
    if "active" in query:
        accounts = accounts.filter(is_active=query["active"])
    return cursor_page(accounts, query=query, user=user, resource="accounts", serialize=account_snapshots)


def recovery_page(user, query):
    requests = RecoveryRequest.objects.filter(resolved_at__isnull=True).select_related("user", "user__worker")
    requests = search_people(requests, query["search"], "user__worker__")
    return cursor_page(
        requests, query=query, user=user, resource="recoveries", oldest=True,
        serialize=lambda rows: [
            {"id": entry.pk, "name": f"{entry.user.worker.first_names} {entry.user.worker.last_names}",
             "email": entry.user.worker.email, "requested_at": entry.requested_at}
            for entry in rows
        ],
    )
