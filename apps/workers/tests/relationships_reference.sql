CREATE TABLE auditoria_evento (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 proyecto_id BIGINT NULL,
 entidad VARCHAR(50) NOT NULL,
 clave VARCHAR(160) NOT NULL,
 accion ENUM('INSERT','UPDATE','DELETE') NOT NULL,
 usuario_id BIGINT NOT NULL,
 cuenta_bd VARCHAR(288) NOT NULL,
 fecha_utc DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
 anterior JSON NULL,
 nuevo JSON NULL,
 FOREIGN KEY (proyecto_id) REFERENCES proyecto(id),
 FOREIGN KEY (usuario_id) REFERENCES usuario(id)
) ENGINE=InnoDB
@@
CREATE PROCEDURE sp_registrar_auditoria(IN p_proyecto BIGINT UNSIGNED,IN p_entidad VARCHAR(50),
 IN p_clave VARCHAR(160),IN p_accion VARCHAR(6),IN p_anterior JSON,IN p_nuevo JSON)
BEGIN
 IF @gm_usuario_id IS NULL OR NOT EXISTS(SELECT 1 FROM usuario WHERE id=@gm_usuario_id AND activo=1) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Establezca @gm_usuario_id con una cuenta activa autenticada.';
 END IF;
 INSERT INTO auditoria_evento(proyecto_id,entidad,clave,accion,usuario_id,cuenta_bd,anterior,nuevo)
 VALUES(p_proyecto,p_entidad,p_clave,p_accion,@gm_usuario_id,USER(),p_anterior,p_nuevo);
END
@@
CREATE TRIGGER ai_equipo_miembro_audit AFTER INSERT ON equipo_miembro FOR EACH ROW BEGIN
 CALL sp_registrar_auditoria(NULL,'equipo_miembro',CONCAT_WS(':',NEW.id),'INSERT',NULL,JSON_OBJECT('id',NEW.id,'equipo_id',NEW.equipo_id,'trabajador_id',NEW.trabajador_id,'fecha_ingreso',NEW.fecha_ingreso,'fecha_salida',NEW.fecha_salida));
END
@@
CREATE TRIGGER au_equipo_miembro_audit AFTER UPDATE ON equipo_miembro FOR EACH ROW BEGIN
 CALL sp_registrar_auditoria(NULL,'equipo_miembro',CONCAT_WS(':',NEW.id),'UPDATE',JSON_OBJECT('id',OLD.id,'equipo_id',OLD.equipo_id,'trabajador_id',OLD.trabajador_id,'fecha_ingreso',OLD.fecha_ingreso,'fecha_salida',OLD.fecha_salida),JSON_OBJECT('id',NEW.id,'equipo_id',NEW.equipo_id,'trabajador_id',NEW.trabajador_id,'fecha_ingreso',NEW.fecha_ingreso,'fecha_salida',NEW.fecha_salida));
END
@@
CREATE TRIGGER bd_equipo_miembro_conservar BEFORE DELETE ON equipo_miembro FOR EACH ROW BEGIN
 SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Conservar historial: archive, anule o cierre el registro; no lo elimine.';
END
@@
ALTER TABLE equipo_miembro ADD CONSTRAINT qa_historical_unique UNIQUE(equipo_id,trabajador_id,fecha_ingreso),
 ADD CONSTRAINT qa_membership_dates CHECK(fecha_salida IS NULL OR fecha_salida >= fecha_ingreso)
@@
ALTER TABLE trabajador_especialidad ADD CONSTRAINT qa_specialty_level CHECK(nivel BETWEEN 1 AND 3)
