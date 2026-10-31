from datetime import timedelta

from django.utils import timezone

from apps.authentication.models import RecoveryRequest
from apps.projects.models import Project, ProjectMember


def seed_lists(fixture):
    projects = [
        Project(code=f"PAGE-{index:03}", name=f"Proyecto QA {index:03}", description="Datos sintéticos de paginación",
                area=fixture.area, type_code="GENERAL", start_date=fixture.today, available=True, worker_create=True)
        for index in range(235)
    ]
    Project.objects.bulk_create(projects)
    projects = list(Project.objects.filter(code__startswith="PAGE-").order_by("pk"))
    ProjectMember.objects.bulk_create([ProjectMember(project=project, worker=fixture.worker.worker) for project in projects])
    for index in range(5):
        Project.objects.create(code=f"HIDDEN-{index}", name=f"Proyecto fuera de ámbito QA {index}",
                               area=fixture.other_area, type_code="GENERAL", start_date=fixture.today, available=False)
    people = []
    for index in range(76):
        user = fixture.account(f"QA{index:03}", "TRABAJADOR", fixture.area, active=index % 10 != 0)
        user.worker.first_names = "Persona QA"
        user.worker.save(update_fields=["first_names"])
        people.append(user)
    recipients = [fixture.worker, fixture.outsider, *[user for user in people if user.is_active]]
    RecoveryRequest.objects.bulk_create([RecoveryRequest(user=user) for user in recipients])
    RecoveryRequest.objects.update(requested_at=timezone.now() - timedelta(days=3))
    resolved = RecoveryRequest.objects.create(user=fixture.worker, resolved_at=timezone.now())
    return {
        "first_project": projects[0].pk, "last_project": projects[-1].pk,
        "first_account": people[0].pk, "last_account": people[-1].pk,
        "last_recovery": RecoveryRequest.objects.filter(user=people[-1], resolved_at__isnull=True).get().pk,
        "resolved_recovery": resolved.pk,
        "project_count": Project.objects.count(), "account_count": len(people) + 3,
        "recovery_count": len(recipients), "area": fixture.area.pk,
    }
