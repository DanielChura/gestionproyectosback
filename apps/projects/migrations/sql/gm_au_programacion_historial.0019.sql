CREATE TRIGGER gm_au_programacion_historial AFTER UPDATE ON actividad FOR EACH ROW
BEGIN
 DECLARE autor VARCHAR(225);
 IF @gm_usuario_id IS NOT NULL THEN
  SELECT CONCAT(w.nombres,' ',w.apellidos) INTO autor FROM usuario u JOIN trabajador w ON w.id=u.trabajador_id WHERE u.id=@gm_usuario_id;
  IF NOT(OLD.precision_calendario<=>NEW.precision_calendario) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
   VALUES(NEW.proyecto_id,NEW.id,COALESCE(@gm_operacion_id,UUID()),@gm_usuario_id,autor,'changed','calendar_precision',JSON_EXTRACT(JSON_OBJECT('v',OLD.precision_calendario),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.precision_calendario),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.hora_inicio_planificada<=>NEW.hora_inicio_planificada) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
   VALUES(NEW.proyecto_id,NEW.id,COALESCE(@gm_operacion_id,UUID()),@gm_usuario_id,autor,'changed','start_time',JSON_EXTRACT(JSON_OBJECT('v',OLD.hora_inicio_planificada),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.hora_inicio_planificada),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.hora_fin_planificada<=>NEW.hora_fin_planificada) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
   VALUES(NEW.proyecto_id,NEW.id,COALESCE(@gm_operacion_id,UUID()),@gm_usuario_id,autor,'changed','due_time',JSON_EXTRACT(JSON_OBJECT('v',OLD.hora_fin_planificada),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.hora_fin_planificada),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.inicio_real_en<=>NEW.inicio_real_en) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
   VALUES(NEW.proyecto_id,NEW.id,COALESCE(@gm_operacion_id,UUID()),@gm_usuario_id,autor,'changed','actual_started_at',JSON_EXTRACT(JSON_OBJECT('v',OLD.inicio_real_en),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.inicio_real_en),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.fin_real_en<=>NEW.fin_real_en) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
   VALUES(NEW.proyecto_id,NEW.id,COALESCE(@gm_operacion_id,UUID()),@gm_usuario_id,autor,'changed','actual_finished_at',JSON_EXTRACT(JSON_OBJECT('v',OLD.fin_real_en),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.fin_real_en),'$.v'),UTC_TIMESTAMP(6));
  END IF;
 END IF;
END
