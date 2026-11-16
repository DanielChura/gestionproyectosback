CREATE PROCEDURE gm_validar_limites_dependencia(IN p_tipo VARCHAR(2), IN a_inicio DATETIME(6), IN a_fin DATETIME(6),
 IN b_inicio DATETIME(6), IN b_fin DATETIME(6), IN a_real_inicio DATETIME(6), IN a_real_fin DATETIME(6),
 IN b_real_inicio DATETIME(6), IN b_real_fin DATETIME(6)) SQL SECURITY INVOKER
BEGIN
 DECLARE requisito DATETIME(6);
 DECLARE condicionado DATETIME(6);
 SET requisito=IF(LEFT(p_tipo,1)='F',a_fin,a_inicio);
 SET condicionado=IF(RIGHT(p_tipo,1)='S',b_inicio,b_fin);
 IF requisito IS NULL OR condicionado IS NULL OR condicionado<requisito THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Dependencia incompatible: define y ajusta los extremos planificados segun el tipo.';
 END IF;
 SET requisito=IF(LEFT(p_tipo,1)='F',a_real_fin,a_real_inicio);
 SET condicionado=IF(RIGHT(p_tipo,1)='S',b_real_inicio,b_real_fin);
 IF condicionado IS NOT NULL AND (requisito IS NULL OR condicionado<requisito) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Dependencia incompatible: el evento real del requisito no se ha cumplido.';
 END IF;
END
