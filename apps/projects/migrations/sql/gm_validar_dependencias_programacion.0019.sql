CREATE PROCEDURE gm_validar_dependencias_programacion(IN p_proyecto BIGINT UNSIGNED, IN p_id BIGINT UNSIGNED,
 IN p_inicio DATETIME(6), IN p_fin DATETIME(6), IN p_real_inicio DATETIME(6), IN p_real_fin DATETIME(6)) SQL SECURITY INVOKER
BEGIN
 DECLARE terminado BOOL DEFAULT FALSE;
 DECLARE tipo VARCHAR(2);
 DECLARE ai,af,bi,bf,ari,arf,bri,brf DATETIME(6);
 DECLARE enlaces CURSOR FOR SELECT d.tipo,
 IF(a.id=p_id,p_inicio,TIMESTAMP(a.fecha_inicio,COALESCE(a.hora_inicio_planificada,'00:00:00'))),
 IF(a.id=p_id,p_fin,IF(a.fecha_fin IS NULL,NULL,IF(a.precision_calendario='day' OR TIME_TO_SEC(a.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(a.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(a.fecha_fin,a.hora_fin_planificada)))),
 IF(b.id=p_id,p_inicio,TIMESTAMP(b.fecha_inicio,COALESCE(b.hora_inicio_planificada,'00:00:00'))),
 IF(b.id=p_id,p_fin,IF(b.fecha_fin IS NULL,NULL,IF(b.precision_calendario='day' OR TIME_TO_SEC(b.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(b.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(b.fecha_fin,b.hora_fin_planificada)))),
 IF(a.id=p_id,p_real_inicio,a.inicio_real_en),
 IF(a.id=p_id,p_real_fin,a.fin_real_en),
 IF(b.id=p_id,p_real_inicio,b.inicio_real_en),
 IF(b.id=p_id,p_real_fin,b.fin_real_en)
 FROM dependencia_actividad d JOIN actividad a ON a.id=d.predecesora_id JOIN actividad b ON b.id=d.sucesora_id
 WHERE d.proyecto_id=p_proyecto AND d.tipo IS NOT NULL AND (a.id=p_id OR b.id=p_id);
 DECLARE CONTINUE HANDLER FOR NOT FOUND SET terminado=TRUE;
 OPEN enlaces;
 lectura: LOOP
  FETCH enlaces INTO tipo,ai,af,bi,bf,ari,arf,bri,brf;
  IF terminado THEN LEAVE lectura; END IF;
  CALL gm_validar_limites_dependencia(tipo,ai,af,bi,bf,ari,arf,bri,brf);
 END LOOP;
 CLOSE enlaces;
END
