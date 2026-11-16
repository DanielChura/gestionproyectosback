CREATE PROCEDURE gm_validar_dependencias_programacion(IN p_proyecto BIGINT UNSIGNED, IN p_id BIGINT UNSIGNED,
 IN p_inicio DATETIME(6), IN p_fin DATETIME(6), IN p_real_inicio DATETIME(6), IN p_real_fin DATETIME(6)) SQL SECURITY INVOKER
BEGIN
 DECLARE terminado BOOL DEFAULT FALSE;
 DECLARE tipo VARCHAR(2);
 DECLARE ai,af,bi,bf,ari,arf,bri,brf DATETIME(6);
 DECLARE antiguo_inicio,antiguo_fin,antiguo_real_inicio,antiguo_real_fin DATETIME(6);
 DECLARE requisito,condicionado,anterior DATETIME(6);
 DECLARE predecesora,sucesora BIGINT UNSIGNED;
 DECLARE requisito_anterior,condicionado_anterior DATETIME(6);
 DECLARE plan_valido,eventos_validos,requisito_cambiado BOOL DEFAULT FALSE;
 DECLARE estado_origen,estado_destino VARCHAR(20);
 DECLARE calendario_cambiado,eventual BOOL DEFAULT FALSE;
 DECLARE enlaces CURSOR FOR SELECT d.tipo,
 IF(a.id=p_id,p_inicio,TIMESTAMP(a.fecha_inicio,COALESCE(a.hora_inicio_planificada,'00:00:00'))),
 IF(a.id=p_id,p_fin,IF(a.fecha_fin IS NULL,NULL,IF(a.precision_calendario='day' OR TIME_TO_SEC(a.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(a.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(a.fecha_fin,a.hora_fin_planificada)))),
 IF(b.id=p_id,p_inicio,TIMESTAMP(b.fecha_inicio,COALESCE(b.hora_inicio_planificada,'00:00:00'))),
 IF(b.id=p_id,p_fin,IF(b.fecha_fin IS NULL,NULL,IF(b.precision_calendario='day' OR TIME_TO_SEC(b.hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(b.fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(b.fecha_fin,b.hora_fin_planificada)))),
 IF(a.id=p_id,p_real_inicio,a.inicio_real_en),IF(a.id=p_id,p_real_fin,a.fin_real_en),
 IF(b.id=p_id,p_real_inicio,b.inicio_real_en),IF(b.id=p_id,p_real_fin,b.fin_real_en),a.id,b.id,a.estado_codigo,b.estado_codigo
 FROM dependencia_actividad d JOIN actividad a ON a.id=d.predecesora_id JOIN actividad b ON b.id=d.sucesora_id
 WHERE d.proyecto_id=p_proyecto AND d.tipo IS NOT NULL AND (a.id=p_id OR b.id=p_id);
 DECLARE CONTINUE HANDLER FOR NOT FOUND SET terminado=TRUE;
 SELECT TIMESTAMP(fecha_inicio,COALESCE(hora_inicio_planificada,'00:00:00')),
 IF(fecha_fin IS NULL,NULL,IF(precision_calendario='day' OR TIME_TO_SEC(hora_fin_planificada)=0,TIMESTAMP(DATE_ADD(fecha_fin,INTERVAL 1 DAY),'00:00:00'),TIMESTAMP(fecha_fin,hora_fin_planificada))),inicio_real_en,fin_real_en
 INTO antiguo_inicio,antiguo_fin,antiguo_real_inicio,antiguo_real_fin FROM actividad WHERE id=p_id;
 SET calendario_cambiado=NOT(p_inicio<=>antiguo_inicio) OR NOT(p_fin<=>antiguo_fin);
 OPEN enlaces;
 lectura: LOOP
  FETCH enlaces INTO tipo,ai,af,bi,bf,ari,arf,bri,brf,predecesora,sucesora,estado_origen,estado_destino;
  IF terminado THEN LEAVE lectura; END IF;
  SET eventual=IF(RIGHT(tipo,1)='S',bri IS NULL AND (estado_destino IN ('PENDIENTE','BLOQUEADA') OR fn_regla_estado_actividad(p_proyecto,estado_destino)='PENDIENTE'),brf IS NULL AND estado_destino<>'CANCELADA' AND fn_regla_estado_actividad(p_proyecto,estado_destino)<>'COMPLETADA');
  SET requisito_anterior=IF(predecesora=p_id,IF(LEFT(tipo,1)='F',antiguo_fin,antiguo_inicio),IF(LEFT(tipo,1)='F',af,ai));
  SET condicionado_anterior=IF(sucesora=p_id,IF(RIGHT(tipo,1)='S',antiguo_inicio,antiguo_fin),IF(RIGHT(tipo,1)='S',bi,bf));
  SET plan_valido=requisito_anterior IS NOT NULL AND condicionado_anterior IS NOT NULL AND requisito_anterior<=condicionado_anterior;
  IF calendario_cambiado AND (eventual OR plan_valido) THEN
   CALL gm_validar_limites_dependencia(tipo,ai,af,bi,bf,NULL,NULL,NULL,NULL);
  END IF;
  SET requisito=IF(LEFT(tipo,1)='F',arf,ari);
  SET condicionado=IF(RIGHT(tipo,1)='S',bri,brf);
  SET requisito_anterior=IF(predecesora=p_id,IF(LEFT(tipo,1)='F',antiguo_real_fin,antiguo_real_inicio),requisito);
  SET condicionado_anterior=IF(sucesora=p_id,IF(RIGHT(tipo,1)='S',antiguo_real_inicio,antiguo_real_fin),condicionado);
  SET eventos_validos=condicionado_anterior IS NULL OR requisito_anterior IS NOT NULL AND requisito_anterior<=condicionado_anterior AND NOT(LEFT(tipo,1)='F' AND estado_origen='CANCELADA');
  SET requisito_cambiado=predecesora=p_id AND NOT(requisito<=>requisito_anterior);
  IF (sucesora=p_id AND condicionado IS NOT NULL AND NOT(condicionado<=>condicionado_anterior) OR requisito_cambiado AND eventos_validos) AND condicionado IS NOT NULL AND
    (requisito IS NULL OR requisito>condicionado OR LEFT(tipo,1)='F' AND estado_origen='CANCELADA') THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Dependencia incompatible: el evento real del requisito no se ha cumplido.';
  END IF;
 END LOOP;
 CLOSE enlaces;
END
