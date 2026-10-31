from copy import deepcopy
from importlib import import_module
import unittest


policy = import_module("apps.projects.migrations.0016_task_state_policy_consistency")
ANSI_MODE = "ANSI_QUOTES,STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION"


def ansi_definition(sql, name):
    prefix = f'CREATE DEFINER="qa_state"@"localhost" PROCEDURE "{name}"'
    return {
        "Create Procedure": sql.replace(f"CREATE PROCEDURE `{name}`", prefix, 1),
        "sql_mode": ANSI_MODE,
        "character_set_client": "utf8mb4",
        "collation_connection": "utf8mb4_0900_ai_ci",
        "Database Collation": "utf8mb4_0900_ai_ci",
    }


class StatePolicyCompatibilityTests(unittest.TestCase):
    def test_reviewed_0010_main_and_0014_mode_accept_ansi_headers(self):
        for name, sql in (
            (policy.direct.NAME, policy.baseline.source(policy.direct.NAME)),
            (policy.direct.MODE, policy.direct.source()),
        ):
            with self.subTest(name=name):
                definition = ansi_definition(sql, name)
                original = deepcopy(definition)
                policy.reviewed(definition, name)
                self.assertEqual(definition, original)
                self.assertEqual(
                    policy.normalized_definition(definition, name),
                    policy.normalized_definition({"Create Procedure": sql}, name),
                )

    def test_previous_accepted_variants_and_installed_sources_still_pass(self):
        for name in policy.NAMES:
            for sql in (
                policy.historical(name), policy.historical(name, True),
                policy.historical(name, True, True), policy.source(name),
            ):
                with self.subTest(name=name, source=sql[:60]):
                    policy.reviewed({"Create Procedure": sql}, name)
                    policy.reviewed(ansi_definition(sql, name), name)
            policy.reviewed(ansi_definition(policy.source(name), name), name, installed=True)

    def test_old_main_is_accepted_only_before_installation(self):
        name = policy.direct.NAME
        with self.assertRaises(RuntimeError):
            policy.reviewed(ansi_definition(policy.baseline.source(name), name), name, installed=True)

    def test_double_quoted_identifiers_require_recorded_ansi_quotes(self):
        name = policy.direct.NAME
        definition = ansi_definition(policy.source(name), name)
        for mode in ("", "STRICT_ALL_TABLES"):
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "ANSI_QUOTES"):
                policy.reviewed({**definition, "sql_mode": mode}, name)

    def test_security_signature_body_and_message_drift_still_fail(self):
        name = policy.direct.NAME
        definition = ansi_definition(policy.baseline.source(name), name)
        for old, new in (
            ("SQL SECURITY INVOKER", "SQL SECURITY DEFINER"),
            ("p_destino VARCHAR(20)", "p_destino VARCHAR(21)"),
            ("v_administrador=0", "v_administrador=1"),
            ("Estado fuera del proyecto", "Estado autorizado"),
            ("UPDATE actividad SET estado_codigo=p_destino", "UPDATE actividad SET estado_codigo=v_origen"),
        ):
            with self.subTest(change=old), self.assertRaisesRegex(RuntimeError, name):
                self.assertIn(old, definition["Create Procedure"])
                policy.reviewed({**definition, "Create Procedure": definition["Create Procedure"].replace(old, new)}, name)

    def test_identifier_normalization_does_not_rewrite_quoted_body_content(self):
        name = policy.direct.NAME
        definition = ansi_definition(policy.source(name), name)
        changed = {**definition, "Create Procedure": definition["Create Procedure"].replace(
            "'Estado fuera del proyecto o no habilitado.'", '"Estado fuera del proyecto o no habilitado."',
        )}
        self.assertNotEqual(policy.normalized_definition(changed, name), policy.normalized_definition(definition, name))
        with self.assertRaises(RuntimeError):
            policy.reviewed(changed, name)

    def test_wrong_routine_name_and_extra_statement_fail(self):
        name = policy.direct.NAME
        definition = ansi_definition(policy.source(name), name)
        for sql in (
            definition["Create Procedure"].replace(f'PROCEDURE "{name}"', 'PROCEDURE "other"'),
            "SELECT 1; " + definition["Create Procedure"],
            definition["Create Procedure"].replace("END", "SELECT 1; END"),
        ):
            with self.subTest(sql=sql[:60]), self.assertRaises(RuntimeError):
                policy.reviewed({**definition, "Create Procedure": sql}, name)

    def test_definer_is_preserved_with_both_mysql_quote_styles(self):
        name = policy.direct.NAME
        definition = ansi_definition(policy.source(name), name)
        rendered = policy.with_definer(policy.source(name), definition, name)
        self.assertTrue(rendered.startswith('CREATE DEFINER="qa_state"@"localhost" PROCEDURE'))
        definition["Create Procedure"] = definition["Create Procedure"].replace(
            '"qa_state"@"localhost"', "`qa_state`@`localhost`",
        )
        self.assertTrue(policy.with_definer(policy.source(name), definition, name).startswith(
            "CREATE DEFINER=`qa_state`@`localhost` PROCEDURE",
        ))
        self.assertEqual(policy.with_definer(
            policy.source(name), {"Create Procedure": policy.source(name)}, name,
        ), policy.source(name))
