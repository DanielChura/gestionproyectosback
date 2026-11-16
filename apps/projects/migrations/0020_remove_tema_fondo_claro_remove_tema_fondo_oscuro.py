from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0019_task_scheduling_dependencies'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name='tema',
                    name='fondo_claro',
                ),
                migrations.RemoveField(
                    model_name='tema',
                    name='fondo_oscuro',
                ),
            ],
        ),
    ]
