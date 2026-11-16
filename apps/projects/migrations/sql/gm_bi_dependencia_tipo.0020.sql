CREATE TRIGGER gm_bi_dependencia_tipo BEFORE INSERT ON dependencia_actividad FOR EACH ROW
BEGIN
 DECLARE v_lock BIGINT UNSIGNED;
 DECLARE ciclo INT DEFAULT 0;
 SELECT id INTO v_lock FROM proyecto WHERE id=NEW.proyecto_id FOR UPDATE;
 IF NEW.tipo IS NULL AND NOT(@gm_dependency_restore<=>1) OR NEW.tipo IS NOT NULL AND NEW.tipo NOT IN ('FS','SS','FF','SF') THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Las relaciones nuevas o convertidas requieren tipo explicito.';
 END IF;
 IF NEW.predecesora_id=NEW.sucesora_id OR
 (SELECT COUNT(*) FROM actividad a WHERE a.id IN (NEW.predecesora_id,NEW.sucesora_id) AND a.proyecto_id=NEW.proyecto_id AND a.archivada=0)<>2 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Usa dos tareas distintas del mismo proyecto.';
 END IF;
 WITH RECURSIVE alcanzables(id) AS (
 SELECT NEW.sucesora_id UNION DISTINCT SELECT d.sucesora_id FROM dependencia_actividad d JOIN alcanzables a ON a.id=d.predecesora_id WHERE d.proyecto_id=NEW.proyecto_id
 ) SELECT COUNT(*) INTO ciclo FROM alcanzables WHERE id=NEW.predecesora_id;
 IF ciclo>0 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='La dependencia generaria un ciclo.'; END IF;
END
