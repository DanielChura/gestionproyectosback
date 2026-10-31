CREATE TRIGGER gm_bu_actividad_calendario BEFORE UPDATE ON actividad FOR EACH ROW
BEGIN
 IF NOT (NEW.fecha_inicio <=> OLD.fecha_inicio) OR NOT (NEW.fecha_fin <=> OLD.fecha_fin) THEN
  IF OLD.calendario_revision >= 9223372036854775807 THEN
   SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Calendar revision exhausted; preserve and review.';
  END IF;
  SET NEW.calendario_revision = OLD.calendario_revision + 1;
 ELSE
  SET NEW.calendario_revision = OLD.calendario_revision;
 END IF;
END

