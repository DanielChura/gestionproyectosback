CREATE PROCEDURE `sp_validar_estructura_actividad`(IN p_id BIGINT UNSIGNED,IN p_proyecto BIGINT UNSIGNED,
 IN p_fase BIGINT UNSIGNED,IN p_padre BIGINT UNSIGNED,IN p_inicio DATE,IN p_fin DATE)
    SQL SECURITY INVOKER
BEGIN
 DECLARE v_lock BIGINT UNSIGNED DEFAULT NULL;
 DECLARE v_ciclo INT DEFAULT 0;
 SELECT id INTO v_lock FROM proyecto WHERE id=p_proyecto FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM proyecto p WHERE p.id=p_proyecto AND p.archivado=0
 AND p.estado_codigo NOT IN ('FINALIZADO','CANCELADO') AND p_inicio IS NOT NULL
 AND (p_fin IS NULL OR p_fin>=p_inicio)
 AND (p.fecha_inicio IS NULL OR p_inicio>=p.fecha_inicio)
 AND (p.fecha_fin IS NULL OR (p_inicio<=p.fecha_fin AND (p_fin IS NULL OR p_fin<=p.fecha_fin)))) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Actividad fuera de fechas o proyecto no editable.';
 END IF;
 IF p_fase IS NOT NULL AND NOT EXISTS(SELECT 1 FROM fase f
 WHERE f.id=p_fase AND f.proyecto_id=p_proyecto AND f.archivada=0
 AND p_inicio>=f.fecha_inicio AND p_inicio<=f.fecha_fin AND (p_fin IS NULL OR p_fin<=f.fecha_fin)) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Fase no disponible o fechas fuera de su periodo.';
 END IF;
 IF p_padre IS NOT NULL THEN
  IF p_padre=p_id OR NOT EXISTS(SELECT 1 FROM actividad WHERE id=p_padre AND proyecto_id=p_proyecto AND (fase_id<=>p_fase)
   AND archivada=0 AND (fn_regla_estado_actividad(p_proyecto,estado_codigo) NOT IN ('COMPLETADA','CANCELADA')
   OR EXISTS(SELECT 1 FROM actividad existente WHERE existente.id=p_id AND existente.padre_id=p_padre AND (existente.fase_id<=>p_fase) AND existente.proyecto_id=p_proyecto))
   AND p_inicio>=fecha_inicio AND (fecha_fin IS NULL OR (p_inicio<=fecha_fin AND (p_fin IS NULL OR p_fin<=fecha_fin)))) THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Padre invalido, cerrado o fechas de subtarea fuera de su padre.';
  END IF;
  WITH RECURSIVE ancestros(id,padre_id) AS (
   SELECT id,padre_id FROM actividad WHERE id=p_padre
   UNION DISTINCT SELECT a.id,a.padre_id FROM actividad a JOIN ancestros r ON a.id=r.padre_id
  ) SELECT COUNT(*) INTO v_ciclo FROM ancestros WHERE id=p_id;
  IF v_ciclo>0 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='La jerarquia de actividades no admite ciclos.'; END IF;
  IF EXISTS(SELECT 1 FROM registro_tiempo WHERE actividad_id=p_padre AND anulado=0) THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Un padre no puede tener horas reales propias.';
  END IF;
 END IF;
 IF EXISTS(SELECT 1 FROM actividad WHERE padre_id=p_id AND archivada=0
 AND (fecha_inicio<p_inicio OR (p_fin IS NOT NULL AND (fecha_fin>p_fin OR fecha_inicio>p_fin)))) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Las nuevas fechas dejarian subtareas fuera del periodo padre.';
 END IF;
END
