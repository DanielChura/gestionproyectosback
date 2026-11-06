CREATE TRIGGER gm_au_actividad_historial AFTER UPDATE ON actividad FOR EACH ROW
BEGIN
 DECLARE v_autor VARCHAR(225);
 DECLARE v_operacion CHAR(36);
 DECLARE v_anterior JSON;
 DECLARE v_nuevo JSON;
 IF @gm_usuario_id IS NOT NULL THEN
  SELECT CONCAT(w.nombres,' ',w.apellidos) INTO v_autor FROM usuario u JOIN trabajador w ON w.id=u.trabajador_id WHERE u.id=@gm_usuario_id AND u.activo=1;
  IF v_autor IS NULL THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Autor de actividad no disponible'; END IF;
  SET v_operacion=COALESCE(@gm_operacion_id,UUID());
  IF NOT(OLD.nombre<=>NEW.nombre) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
VALUES(NEW.proyecto_id,NEW.id,v_operacion,@gm_usuario_id,v_autor,'changed','name',JSON_EXTRACT(JSON_OBJECT('v',OLD.nombre),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.nombre),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.prioridad<=>NEW.prioridad) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
VALUES(NEW.proyecto_id,NEW.id,v_operacion,@gm_usuario_id,v_autor,'changed','priority',JSON_EXTRACT(JSON_OBJECT('v',OLD.prioridad),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.prioridad),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.fecha_inicio<=>NEW.fecha_inicio) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
VALUES(NEW.proyecto_id,NEW.id,v_operacion,@gm_usuario_id,v_autor,'changed','start_date',JSON_EXTRACT(JSON_OBJECT('v',OLD.fecha_inicio),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.fecha_inicio),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.fecha_fin<=>NEW.fecha_fin) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
VALUES(NEW.proyecto_id,NEW.id,v_operacion,@gm_usuario_id,v_autor,'changed','due_date',JSON_EXTRACT(JSON_OBJECT('v',OLD.fecha_fin),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.fecha_fin),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.duracion_estimada_minutos<=>NEW.duracion_estimada_minutos) THEN
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
VALUES(NEW.proyecto_id,NEW.id,v_operacion,@gm_usuario_id,v_autor,'changed','estimated_minutes',JSON_EXTRACT(JSON_OBJECT('v',OLD.duracion_estimada_minutos),'$.v'),JSON_EXTRACT(JSON_OBJECT('v',NEW.duracion_estimada_minutos),'$.v'),UTC_TIMESTAMP(6));
  END IF;
  IF NOT(OLD.estado_codigo<=>NEW.estado_codigo) THEN
   SET v_anterior=COALESCE(JSON_EXTRACT(CONVERT(@gm_estados_anteriores USING utf8mb4),CONCAT('$."',OLD.estado_codigo,'"')),
    (SELECT JSON_OBJECT('code',estado_codigo,'name',nombre,'category',categoria,'tema_id',tema_id) FROM proyecto_estado_actividad WHERE proyecto_id=OLD.proyecto_id AND estado_codigo=OLD.estado_codigo),
    JSON_OBJECT('code',OLD.estado_codigo,'name',(SELECT nombre FROM estado_actividad WHERE codigo=OLD.estado_codigo)));
   SET v_nuevo=COALESCE(
    (SELECT JSON_OBJECT('code',estado_codigo,'name',nombre,'category',categoria,'tema_id',tema_id) FROM proyecto_estado_actividad WHERE proyecto_id=NEW.proyecto_id AND estado_codigo=NEW.estado_codigo),
    JSON_OBJECT('code',NEW.estado_codigo,'name',(SELECT nombre FROM estado_actividad WHERE codigo=NEW.estado_codigo)));
   INSERT INTO actividad_historial(proyecto_id,actividad_id,operacion_id,autor_id,autor_nombre,accion,campo,valor_anterior,valor_nuevo,creado_en)
VALUES(NEW.proyecto_id,NEW.id,v_operacion,@gm_usuario_id,v_autor,'changed','state_code',v_anterior,v_nuevo,UTC_TIMESTAMP(6));
  END IF;
 END IF;
END