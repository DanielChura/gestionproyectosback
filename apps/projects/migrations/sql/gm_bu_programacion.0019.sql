CREATE TRIGGER gm_bu_programacion BEFORE UPDATE ON actividad FOR EACH ROW
BEGIN
 DECLARE v_lock BIGINT UNSIGNED;
 DECLARE v_inicio,v_fin DATETIME(6);
 SELECT id INTO v_lock FROM proyecto WHERE id=NEW.proyecto_id FOR UPDATE;
 IF NEW.precision_calendario='day' AND (NEW.hora_inicio_planificada IS NOT NULL OR NEW.hora_fin_planificada IS NOT NULL)
 OR NEW.precision_calendario='hour' AND (NEW.hora_inicio_planificada IS NULL OR (NEW.fecha_fin IS NULL)<>(NEW.hora_fin_planificada IS NULL)) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Calendario incoherente: usa una sola precision y extremos compatibles.';
 END IF;
 SET v_inicio=TIMESTAMP(NEW.fecha_inicio,COALESCE(NEW.hora_inicio_planificada,'00:00:00'));
 SET v_fin=IF(NEW.fecha_fin IS NULL,NULL,IF(NEW.precision_calendario='day' OR TIME_TO_SEC(NEW.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(NEW.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(NEW.fecha_fin,NEW.hora_fin_planificada)));
 IF v_fin IS NOT NULL AND (v_fin<=v_inicio OR NEW.precision_calendario='hour' AND TIMESTAMPDIFF(MINUTE,v_inicio,v_fin)<60) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='El intervalo horario minimo es una hora y el fin es exclusivo.';
 END IF;
 IF NEW.padre_id IS NOT NULL AND EXISTS(SELECT 1 FROM actividad p WHERE p.id=NEW.padre_id AND
 (v_inicio<TIMESTAMP(p.fecha_inicio,COALESCE(p.hora_inicio_planificada,'00:00:00')) OR IF(p.fecha_fin IS NULL,NULL,IF(p.precision_calendario='day' OR TIME_TO_SEC(p.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(p.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(p.fecha_fin,p.hora_fin_planificada))) IS NOT NULL AND (v_inicio>=IF(p.fecha_fin IS NULL,NULL,IF(p.precision_calendario='day' OR TIME_TO_SEC(p.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(p.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(p.fecha_fin,p.hora_fin_planificada))) OR v_fin>IF(p.fecha_fin IS NULL,NULL,IF(p.precision_calendario='day' OR TIME_TO_SEC(p.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(p.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(p.fecha_fin,p.hora_fin_planificada)))))) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='El calendario debe quedar dentro del padre.';
 END IF;
 IF NEW.estado_codigo<>OLD.estado_codigo AND
 (fn_regla_estado_actividad(NEW.proyecto_id,NEW.estado_codigo)<>fn_regla_estado_actividad(OLD.proyecto_id,OLD.estado_codigo)
 OR OLD.estado_codigo IN ('BLOQUEADA','CANCELADA') OR NEW.estado_codigo='CANCELADA') THEN
 IF NEW.estado_codigo='CANCELADA' THEN
  SET NEW.fin_real_en=NULL;
 ELSEIF fn_regla_estado_actividad(NEW.proyecto_id,NEW.estado_codigo)='PENDIENTE' THEN
  SET NEW.inicio_real_en=NULL;
  SET NEW.fin_real_en=NULL;
 ELSEIF fn_regla_estado_actividad(NEW.proyecto_id,NEW.estado_codigo)='COMPLETADA' THEN
  SET NEW.inicio_real_en=COALESCE(NEW.inicio_real_en,UTC_TIMESTAMP(6));
  SET NEW.fin_real_en=COALESCE(NEW.fin_real_en,UTC_TIMESTAMP(6));
 ELSEIF NEW.estado_codigo<>'BLOQUEADA' THEN
  SET NEW.inicio_real_en=COALESCE(NEW.inicio_real_en,UTC_TIMESTAMP(6));
  SET NEW.fin_real_en=NULL;
 END IF;
 END IF;
 IF NOT(NEW.precision_calendario<=>OLD.precision_calendario) OR NOT(NEW.hora_inicio_planificada<=>OLD.hora_inicio_planificada)
 OR NOT(NEW.hora_fin_planificada<=>OLD.hora_fin_planificada) THEN
  IF OLD.calendario_revision>=9223372036854775807 THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Calendar revision exhausted; preserve and review.';
  END IF;
  SET NEW.calendario_revision=OLD.calendario_revision+1;
 END IF;
 IF EXISTS(SELECT 1 FROM actividad h WHERE h.padre_id=NEW.id AND h.archivada=0 AND
 (TIMESTAMP(h.fecha_inicio,COALESCE(h.hora_inicio_planificada,'00:00:00'))<v_inicio OR v_fin IS NOT NULL AND (TIMESTAMP(h.fecha_inicio,COALESCE(h.hora_inicio_planificada,'00:00:00'))>=v_fin OR IF(h.fecha_fin IS NULL,NULL,IF(h.precision_calendario='day' OR TIME_TO_SEC(h.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(h.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(h.fecha_fin,h.hora_fin_planificada)))>v_fin))) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='La propuesta dejaria subtareas fuera del calendario del padre.';
 END IF;
 CALL gm_validar_dependencias_programacion(NEW.proyecto_id,NEW.id,v_inicio,v_fin,NEW.inicio_real_en,NEW.fin_real_en);
END
