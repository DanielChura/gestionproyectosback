# Creación de proyectos: área única y fecha final opcional

Aclaración de contratos vigentes: 29/10/2026. Las secciones históricas de esquema se conservan como antecedentes y no certifican el entorno compartido.

## Contrato de nuevas creaciones

`POST /api/projects/` conserva `area_ids`, obligatorio con exactamente un identificador de área activa. El serializer rechaza ausencia, lista vacía, duplicados y múltiples áreas; el servicio vuelve a comprobar la cardinalidad antes de escribir. La transacción guarda esa área exclusivamente en `proyecto.area_id`. La respuesta mantiene `areas` como una lista de un elemento.

`end_date` acepta una fecha ISO, `null` o ausencia. React envía `null` cuando el control queda vacío. Las cadenas vacías y fechas inválidas se rechazan. Una fecha indicada no puede preceder al inicio asignado por el servidor, incluso si cambia el día entre validación y creación. Sin fecha final, la respuesta devuelve `end_date: null`.

La modalidad, pertenencia de participantes y permisos mantienen sus reglas. `GET /api/projects/workers/?area_ids=<id>` requiere una sola área; un trabajador puede seguir perteneciendo a varias áreas o tener autorización para todas.

## Esquema y operaciones posteriores revisadas localmente

`Project.end_date` y el estado de migraciones ya declaran `proyecto.fecha_fin` nullable. La migración existente `0002_project_schema` contempla `DATE NULL`; no se modifica ni se aplica ninguna migración para este cambio.

`validate_dates` y el SQL conservado de `sp_validar_estructura_actividad` omiten únicamente el límite superior del proyecto cuando `fecha_fin IS NULL`. El contrato vigente de tarea, posterior a 0011, permite fecha límite ausente o null; start_date sigue siendo obligatorio en persistencia y se asigna por defecto al crear. validate_dates conserva la coherencia inicio/límite y los límites del proyecto/padre; el trigger estructural puede añadir la validación de fase donde esté instalado. La restricción de hoy o después se aplica al crear, sin invalidar fechas históricas existentes. Edición, asignación, estados y dependencias mantienen permisos propios. Los endpoints de estado y dependencias ejecutan servicios ORM transaccionales: no llaman directamente a los procedimientos históricos. La política de estado vigente no exige motivo/responsable ni propaga a hijos; las dependencias conservan validación de grafo, sin bloquear estados ni reprogramar fechas. El formulario de tareas no inventa una fecha cuando el proyecto no la tiene.

Esta revisión no consultó Aiven ni ejecutó procedimientos MySQL. Las pruebas ORM usan exclusivamente la configuración SQLite aislada; la revisión de procedimientos es estática.

## Auditoría histórica anterior y retirada versionada

La auditoría posteriormente aportada por el responsable no devolvió inconsistencias. La migración `0007_project_single_area` sustituye las lecturas históricas descritas abajo y retira la tabla tras una nueva validación. El plan vigente, resultados de revisión, tratamientos ante discrepancias y consultas posteriores están en `../migrations/reviews/0007_project_single_area.md`. La siguiente sección conserva el contexto de la revisión previa; no describe el comportamiento actual después de 0007.

No se sabe cuántos proyectos compartidos tienen varias asociaciones o discrepancias con el área principal: no se accedió a Aiven. No se elimina ninguna asociación ni se impone una restricción global sobre datos sin revisarlos.

La lectura histórica se conserva en `project_areas`, `eligible_project_workers`, `project_snapshot` y `visible_projects`. Los procedimientos de cambio de estado consultan `proyecto_area` y usan el área principal como respaldo si no hay asociaciones. Retirar relaciones puede alterar visibilidad, elegibilidad y transiciones; requiere una decisión explícita del responsable de los datos.

Estas consultas de solo lectura quedan preparadas para una revisión posterior autorizada; no se ejecutaron:

```sql
SELECT proyecto_id, COUNT(*) AS cantidad_areas
FROM proyecto_area
GROUP BY proyecto_id
HAVING COUNT(*) > 1;

SELECT p.id, p.area_id, COUNT(pa.area_id) AS cantidad_areas,
       SUM(CASE WHEN pa.area_id = p.area_id THEN 1 ELSE 0 END) AS coincide_principal
FROM proyecto p
LEFT JOIN proyecto_area pa ON pa.proyecto_id = p.id
GROUP BY p.id, p.area_id
HAVING COUNT(pa.area_id) <> 1
    OR SUM(CASE WHEN pa.area_id = p.area_id THEN 1 ELSE 0 END) <> 1;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'proyecto'
  AND COLUMN_NAME = 'fecha_fin';
```

Para garantizar área única también en todos los históricos, primero debe revisarse cada resultado y su impacto en participantes, permisos y SQL. Solo después de una reconciliación aprobada se podrá evaluar una restricción global. Esta entrega aplica la regla exclusivamente a nuevas creaciones, conservando los históricos.
