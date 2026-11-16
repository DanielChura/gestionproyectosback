CREATE TRIGGER gm_bi_dependencia_tipo BEFORE INSERT ON dependencia_actividad FOR EACH ROW
BEGIN
 DECLARE v_lock BIGINT UNSIGNED;
 DECLARE ai,af,bi,bf,ari,arf,bri,brf DATETIME(6);
 DECLARE ciclo INT DEFAULT 0;
 DECLARE estado_destino VARCHAR(20);
 SELECT id INTO v_lock FROM proyecto WHERE id=NEW.proyecto_id FOR UPDATE;
 IF NEW.tipo IS NULL OR NEW.tipo NOT IN ('FS','SS','FF','SF') THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Las relaciones nuevas o convertidas requieren tipo explicito.';
 END IF;
 IF NEW.predecesora_id=NEW.sucesora_id OR
 (SELECT COUNT(*) FROM actividad a WHERE a.id IN (NEW.predecesora_id,NEW.sucesora_id) AND a.proyecto_id=NEW.proyecto_id AND a.padre_id IS NULL AND a.archivada=0)<>2 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Usa dos tareas principales distintas del mismo proyecto.';
 END IF;
 WITH RECURSIVE alcanzables(id) AS (
 SELECT NEW.sucesora_id UNION DISTINCT SELECT d.sucesora_id FROM dependencia_actividad d JOIN alcanzables a ON a.id=d.predecesora_id WHERE d.proyecto_id=NEW.proyecto_id
 ) SELECT COUNT(*) INTO ciclo FROM alcanzables WHERE id=NEW.predecesora_id;
 IF ciclo>0 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='La dependencia generaria un ciclo.'; END IF;
 SELECT TIMESTAMP(a.fecha_inicio,COALESCE(a.hora_inicio_planificada,'00:00:00')),IF(a.fecha_fin IS NULL,NULL,IF(a.precision_calendario='day' OR TIME_TO_SEC(a.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(a.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(a.fecha_fin,a.hora_fin_planificada))),TIMESTAMP(b.fecha_inicio,COALESCE(b.hora_inicio_planificada,'00:00:00')),IF(b.fecha_fin IS NULL,NULL,IF(b.precision_calendario='day' OR TIME_TO_SEC(b.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(b.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(b.fecha_fin,b.hora_fin_planificada))),a.inicio_real_en,a.fin_real_en,b.inicio_real_en,b.fin_real_en
 INTO ai,af,bi,bf,ari,arf,bri,brf FROM actividad a JOIN actividad b ON b.id=NEW.sucesora_id WHERE a.id=NEW.predecesora_id;
 SELECT estado_codigo INTO estado_destino FROM actividad WHERE id=NEW.sucesora_id;
 IF (bri IS NULL AND RIGHT(NEW.tipo,1)='S' AND estado_destino NOT IN ('PENDIENTE','BLOQUEADA','CANCELADA') AND fn_regla_estado_actividad(NEW.proyecto_id,estado_destino)<>'PENDIENTE')
 OR (brf IS NULL AND estado_destino<>'CANCELADA' AND fn_regla_estado_actividad(NEW.proyecto_id,estado_destino)='COMPLETADA') THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='No se puede acreditar el evento real historico para una restriccion retroactiva.';
 END IF;
 CALL gm_validar_limites_dependencia(NEW.tipo,ai,af,bi,bf,ari,arf,bri,brf);
END
