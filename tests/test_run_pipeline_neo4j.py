import importlib.util
import io
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
NEO4J_MAIN = ROOT / "Tool-pg-to-neo4j" / "src" / "main.py"
RUN_PIPELINE = ROOT / "run_pipeline.bat"
REQUIREMENTS = ROOT / "requirements.txt"


def load_neo4j_main():
    """Import the CLI with fake database packages; no driver is contacted."""
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = Mock()

    fake_neo4j = types.ModuleType("neo4j")
    fake_neo4j.GraphDatabase = Mock()

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = Mock()

    spec = importlib.util.spec_from_file_location("pg_to_neo4j_main_test", NEO4J_MAIN)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "psycopg2": fake_psycopg2,
            "neo4j": fake_neo4j,
            "dotenv": fake_dotenv,
        },
    ):
        spec.loader.exec_module(module)
    return module


class RunPipelineNeo4jTest(unittest.TestCase):
    def test_batch_menu_exposes_safe_neo4j_option(self):
        content = RUN_PIPELINE.read_text(encoding="utf-8")

        self.assertIn("N. Sync PostgreSQL -^> Neo4j", content)
        self.assertIn("if errorlevel 14 goto sync_neo4j", content)
        self.assertIn(':sync_neo4j', content)
        self.assertIn('pushd "Tool-pg-to-neo4j"', content)
        self.assertIn('if /I "%PIPELINE_DRY_RUN%"=="1"', content)
        self.assertIn('python -m pip install "neo4j>=5.14.0,<6.0.0"', content)
        self.assertIn("--type full --all", content)
        self.assertIn("neo4j>=", REQUIREMENTS.read_text(encoding="utf-8"))

    def test_all_option_is_non_interactive_and_success_is_propagated(self):
        module = load_neo4j_main()

        with (
            patch.object(module, "acquire") as acquire_mock,
            patch.object(module, "run_sync", return_value=True) as run_sync_mock,
            patch.object(sys, "argv", ["main.py", "--type", "full", "--all"]),
        ):
            result = module.main()

        self.assertEqual(result, 0)
        acquire_mock.assert_called_once_with("pg_to_neo4j")
        run_sync_mock.assert_called_once_with("full", limit=None)

    def test_sync_failure_returns_nonzero_without_database_access(self):
        module = load_neo4j_main()
        module.psycopg2.connect.side_effect = RuntimeError("mock connection failure")

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = module.run_sync("full", limit=10)

        self.assertFalse(result)
        self.assertIn("mock connection failure", output.getvalue())
        self.assertIn("failed", output.getvalue())
        self.assertNotIn("completed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
