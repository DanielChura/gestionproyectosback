CREATE TRIGGER gm_bi_programacion BEFORE INSERT ON actividad FOR EACH ROW
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
END
