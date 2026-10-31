from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.projects.models import TaskCalendarIntent


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        size = options["batch_size"]
        if not 1 <= size <= 10000:
            from django.core.management.base import CommandError
            raise CommandError("batch-size debe estar entre 1 y 10000")
        ids = list(TaskCalendarIntent.objects.filter(expires_at__lte=timezone.now()).exclude(status="expired").order_by("pk").values_list("pk", flat=True)[:size])
        count = TaskCalendarIntent.objects.filter(pk__in=ids, expires_at__lte=timezone.now()).update(status="expired")
        self.stdout.write(f"{count} intenciones marcadas como caducadas; claves conservadas.")

