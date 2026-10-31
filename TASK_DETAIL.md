# Persistencia del detalle de tareas

Actualización de política y documentación: 29/10/2026. Las verificaciones históricas se identifican como tales; el cierre vigente está en [correcciones de base previas a Gantt](docs/reviews/correcciones_base_previas_gantt.md).

## Cambios

`Task.estimated_minutes` añade la columna nullable `actividad.duracion_estimada_minutos`. Minutos enteros positivos, máximo 2147483647; `null` limpia. Se incorpora al snapshot y al PATCH existente, con el mismo permiso efectivo de edición y sin alterar la creación ni los otros campos omitidos.

`TaskRelation` (`actividad_relacion`) almacena la asociación informativa del mismo proyecto. Guarda el par ordenado de IDs y un nombre de máximo 70 caracteres, con clave normalizada mediante `casefold` y capacidad para su expansión Unicode. Valida autoenlace, proyecto, autorización y duplicado equivalente. No participa en dependencias ni en progreso.

`TaskActivity` (`actividad_historial`) guarda tarea/proyecto, UUID de operación, autor autenticado, nombre histórico, acción/campo, valores JSON anteriores/nuevos, elemento relacionado y timestamp UTC. No se expone ninguna ruta para modificar o eliminar eventos. Sus lecturas se restringen a la tarea y a las relaciones autorizadas; no inventa eventos para tareas antiguas.

Servicios: `audit.py` coordina actor y operación, restaura/limpia el contexto y las variables MySQL incluso tras error; `activity.py` toma snapshots y pagina operaciones completas; `task_detail.py` concentra lectura de detalle, subtareas, búsqueda y relaciones. `tasks.py` y `statuses.py` registran las mutaciones en sus transacciones existentes. `presentation.py` añade duración y permiso de relaciones; serializers, vistas y urls incorporan los contratos mínimos.

Las dependencias conservan dirección predecesora → sucesora y validación de grafo/ámbito mediante manage_dependency (ORM y transacción); no se llama sp_agregar_dependencia desde su endpoint. No bloquean hoy los cambios de estado ni reprograman fechas. Los cambios de estado manuales y move usan apply_state_change ORM: destino habilitado y permisos efectivos, sin motivo, responsable o supervisión adicionales y sin propagación a hijos. Las nuevas relaciones se reservan al administrador de un proyecto editable, igual que la gestión existente de dependencias; abrir un detalle no concede esa facultad.

## API

Base: `/api/projects/{project_id}/tasks/{task_id}/`.

| Operación | Contrato |
| --- | --- |
| GET base | `{ task, parent }`, padre solo si es accesible |
| PATCH base | Contrato parcial existente + `estimated_minutes: integer | null` |
| GET `subtasks/?cursor=` | `{ items, total, completed, next_cursor }`, 30 filas, conteos autorizados globales |
| GET `activity/?cursor=` | `{ items: [{ id, events }], next_cursor }`, hasta 20 operaciones completas; cursor de operaciones anteriores |
| GET `related-candidates/?kind=&q=&cursor=` | 30 tareas elegibles, ID descendente, búsqueda de hasta 100 caracteres; `kind=blocks|blocked_by|custom` |
| GET `relations/?cursor=` | Dependencias y relaciones informativas autorizadas, 30 filas |
| POST `relations/` | `{ target_id, name }`, 201 |
| DELETE `relations/{relation_id}/` | Desvincula la relación informativa, 204 |

La gestión de dependencias y las transiciones conservan sus endpoints. No se aceptan autores ni metadatos de actividad enviados por cliente. La paginación del historial utiliza ID de inicio de operación y entrega todos sus eventos autorizados; no agrupa por proximidad temporal. El índice `(task, operation_id, id)` apoya esa lectura.

## Migración 0012 y seguridad operativa

`0012_task_detail` depende de `0011_task_optional_properties`. Añade físicamente la columna, las dos tablas, sus índices/restricciones y el trigger nuevo `gm_au_actividad_historial`. No reemplaza procedimientos ni triggers existentes.

El trigger registra nombre, estado, fechas, prioridad y duración dentro de la transacción MySQL real, incluidos los procedimientos de transición y reasignación de flujo. Los servicios registran creación, responsables, etiquetas y relaciones; no duplican los eventos escalares MySQL. SQLite registra esos escalares desde servicios únicamente en su modo de pruebas explícito.

La reasignación de flujo preserva el snapshot del estado retirado antes de sustituir la configuración. El trigger convierte el contexto JSON a UTF-8 para evitar el error MySQL de cadenas binarias, observado y corregido durante la prueba aislada.

Antes de una aplicación autorizada deben estar aplicadas las dependencias, existir backup verificado, detenerse las escrituras y revisarse los privilegios DDL/TRIGGER de la cuenta ejecutora. Requiere `TASK_DETAIL_MIGRATION_MAINTENANCE=1` en el proceso de migración; el ejemplo de entorno lo mantiene en 0. Desactivarlo al terminar. La aplicación compartida debe actualizarse coordinadamente con el esquema.

El preflight rechaza columnas, tablas o un trigger de detalle que ya existan para no sobrescribir una instalación parcial o ajena. MySQL tiene DDL no atómico: si una ejecución se interrumpe, conservar lo existente y revisar la instalación antes de reintentar; no usar `--fake` para ocultar discrepancias.

La reversión se niega si hay duración, relaciones o historial: exige un plan explícito de preservación. No borrar registros para forzarla. La prueba aislada sí ejecutó reversión completa cuando no había datos y reaplicó la migración, comprobando el esquema real. También verificó tipo `int unsigned`, nulabilidad y rechazo físico de duración inválida.

No se aplicó esta migración a Aiven ni a ninguna base compartida. Su aplicación en el entorno de despliegue queda pendiente de la operación autorizada correspondiente.

## Verificación histórica de la entrega de detalle

Los conteos siguientes pertenecen a aquella ejecución, anterior a la retirada de restricciones de estado y a los bloques de corrección. No son una aprobación del estado final actual. Las menciones a motivo/supervisión y entregables describen lo ensayado entonces; no son requisitos vigentes para cambiar estados. Los helpers SQL conservan pruebas independientes; no prueban que los endpoints los invoquen.

Registro posterior de integración y capturas históricas: [integración GM del 21/10](../frontend/docs/task-detail-verification/project-integrations/README.md). Sus resultados pertenecen a ese corte y no sustituyen las verificaciones del cierre actual.

- 126 pruebas dirigidas SQLite: lógica, contratos, permisos y rollback ORM. No validan SQL MySQL.
- 22 pruebas en MySQL 8.4 nuevo y dedicado: migración real, trigger, procedimientos reales, reasignación con nombres históricos, reversión de transacción externa, duración, permisos, entregables, jerarquía, dependencias, responsables secundarios, motivo/supervisión y Scrum.
- Se actualizaron fixtures manuales para incluir Equipos antes de Proyectos y las tablas nuevas en orden de creación/limpieza.
- Django check y `makemigrations --check --dry-run`: correctos. Ruff no está instalado; el intento mediante el intérprete local terminó en `No module named ruff`, sin instalar nuevas dependencias.
- Script reproducible: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/verify_task_status_mysql.ps1 -TestModule apps.projects.tests.test_task_detail_mysql`.

El contenedor y la base sintética se eliminan al terminar. No se guardaron credenciales, ni se hicieron commit o push.
