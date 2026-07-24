import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MAIN_SCRIPT = ROOT / "pg-to-melisearch" / "main.py"
RUN_PIPELINE = ROOT / "run_pipeline.bat"


def load_main_with_environment(values):
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = Mock()
    fake_psycopg2.OperationalError = RuntimeError
    fake_psycopg2.InterfaceError = RuntimeError

    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = object()

    fake_meilisearch = types.ModuleType("meilisearch")
    fake_meilisearch.Client = Mock()

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = Mock()

    spec = importlib.util.spec_from_file_location(
        "pg_to_meilisearch_config_test",
        MAIN_SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(os.environ, values, clear=True),
        patch.dict(
            sys.modules,
            {
                "psycopg2": fake_psycopg2,
                "psycopg2.extras": fake_extras,
                "meilisearch": fake_meilisearch,
                "dotenv": fake_dotenv,
            },
        ),
    ):
        spec.loader.exec_module(module)

    return module, fake_meilisearch.Client, fake_dotenv.load_dotenv


class MeilisearchConfigTest(unittest.TestCase):
    def setUp(self):
        self.values = {
            "PG_DATABASE": "test_database",
            "PG_USER": "test_user",
            "PG_PASSWORD": "test_password",
            "PG_HOST": "postgres.test",
            "PG_PORT": "5544",
            "MEILI_HOST": "http://meili.test:7700/",
            "MEILI_API_KEY": "test_key",
            "MEILI_TIMEOUT": "45",
            "MEILI_BATCH_SIZE": "250",
            "MEILI_MAX_CONCURRENT_TASKS": "4",
        }

    def test_configuration_is_loaded_from_folder_environment(self):
        module, client_mock, load_dotenv_mock = load_main_with_environment(
            self.values,
        )

        load_dotenv_mock.assert_called_once_with(module.ENV_PATH, override=False)
        self.assertEqual(module.ENV_PATH, ROOT / "pg-to-melisearch" / ".env")
        self.assertEqual(module.PG_CONFIG["dbname"], "test_database")
        self.assertEqual(module.PG_CONFIG["port"], 5544)
        self.assertEqual(module.MEILI_CONFIG["host"], "http://meili.test:7700")
        self.assertEqual(module.MEILI_CONFIG["timeout"], 45)
        self.assertEqual(module.BATCH_SIZE, 250)
        self.assertEqual(module.MAX_CONCURRENT_TASKS, 4)
        client_mock.assert_called_once_with(
            "http://meili.test:7700",
            "test_key",
            timeout=45,
        )

    def test_missing_required_value_is_rejected(self):
        values = dict(self.values)
        values.pop("MEILI_API_KEY")

        with self.assertRaisesRegex(ValueError, "MEILI_API_KEY"):
            load_main_with_environment(values)

    def test_connection_values_are_not_hardcoded_in_source(self):
        source = MAIN_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("load_dotenv(ENV_PATH, override=False)", source)
        self.assertNotRegex(source, r'"password":\s*"[^"]+"')
        self.assertNotRegex(source, r'"api_key":\s*"[^"]+"')

    def test_pipeline_exposes_safe_meilisearch_option(self):
        source = MAIN_SCRIPT.read_text(encoding="utf-8")
        pipeline = RUN_PIPELINE.read_text(encoding="utf-8")

        self.assertIn('acquire("pg_to_meilisearch")', source)
        self.assertIn("L. Sync PostgreSQL -^> Meilisearch", pipeline)
        self.assertIn("if errorlevel 16 goto sync_meilisearch", pipeline)
        self.assertIn(":sync_meilisearch", pipeline)
        self.assertIn('pushd "pg-to-melisearch"', pipeline)
        self.assertIn(
            'python -m pip install -r "pg-to-melisearch\\requirements.txt"',
            pipeline,
        )
        self.assertIn("--dry-run-meilisearch", pipeline)
        self.assertIn("Nhap MEILI de tiep tuc", pipeline)

    def test_sync_failure_returns_nonzero_and_releases_connection(self):
        module, _client_mock, _load_dotenv_mock = load_main_with_environment(
            self.values,
        )
        connection = Mock()

        with (
            patch.object(module, "acquire") as acquire_mock,
            patch.object(module, "clear_stuck_tasks"),
            patch.object(module, "get_pg_connection", return_value=connection),
            patch.object(
                module,
                "sync_large_table_optimized",
                side_effect=RuntimeError("mock sync failure"),
            ),
            patch("builtins.input", return_value="1"),
        ):
            result = module.main()

        self.assertEqual(result, 1)
        acquire_mock.assert_called_once_with("pg_to_meilisearch")
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
