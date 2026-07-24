import importlib.util
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
EMBED_SCRIPT = ROOT / "embedding-tool" / "embed_database.py"
RUN_PIPELINE = ROOT / "run_pipeline.bat"


def load_embedding_module():
    fake_sentence_transformers = types.ModuleType("sentence_transformers")
    fake_sentence_transformers.SentenceTransformer = Mock()

    spec = importlib.util.spec_from_file_location(
        "embed_database_config_test",
        EMBED_SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"sentence_transformers": fake_sentence_transformers},
    ):
        spec.loader.exec_module(module)
    return module, fake_sentence_transformers.SentenceTransformer


class EmbeddingConfigTest(unittest.TestCase):
    def test_import_does_not_initialize_model_or_contain_database_url(self):
        module, model_mock = load_embedding_module()

        self.assertFalse(model_mock.called)
        self.assertEqual(module.ENV_PATH, ROOT / "embedding-tool" / ".env")
        self.assertEqual(module.VERCEL_ENV_PATH, ROOT / ".env.vercel")
        self.assertNotIn(
            "postgresql://",
            EMBED_SCRIPT.read_text(encoding="utf-8"),
        )

    def test_settings_are_loaded_from_environment(self):
        module, _model_mock = load_embedding_module()
        values = {
            "SUPABASE_DB_URL": "postgresql://example.invalid/database",
            "EMBEDDING_MODEL_NAME": "test/model",
            "EMBEDDING_DIMENSION": "768",
            "EMBEDDING_DEVICE": "cpu",
            "EMBEDDING_NORMALIZE": "true",
            "EMBEDDING_DB_CONNECT_TIMEOUT": "12",
        }

        with patch.dict(os.environ, values, clear=True):
            settings = module.load_settings()

        self.assertEqual(settings.database_url, values["SUPABASE_DB_URL"])
        self.assertEqual(settings.provider, "local")
        self.assertEqual(settings.model_name, "test/model")
        self.assertEqual(settings.dimension, 768)
        self.assertEqual(settings.device, "cpu")
        self.assertTrue(settings.normalize)
        self.assertEqual(settings.connect_timeout, 12)

    def test_dimension_override_selects_dimension_specific_model(self):
        module, _model_mock = load_embedding_module()
        values = {
            "SUPABASE_DB_URL": "postgresql://example.invalid/database",
            "EMBEDDING_DIMENSION": "768",
            "EMBEDDING_MODEL_NAME": "test/default-768",
            "EMBEDDING_PROVIDER_3072": "ollama",
            "EMBEDDING_MODEL_NAME_3072": "qwen3-embedding:8b",
            "EMBEDDING_OLLAMA_HOST": "http://ollama.test:11434/",
        }

        with patch.dict(os.environ, values, clear=True):
            settings = module.load_settings(dimension_override=3072)

        self.assertEqual(settings.provider, "ollama")
        self.assertEqual(settings.dimension, 3072)
        self.assertEqual(settings.model_name, "qwen3-embedding:8b")
        self.assertEqual(settings.ollama_host, "http://ollama.test:11434")

    def test_database_url_is_required(self):
        module, _model_mock = load_embedding_module()

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SUPABASE_DB_URL"):
                module.load_settings()

    def test_dry_run_does_not_load_model_or_connect_database(self):
        module, model_mock = load_embedding_module()
        values = {
            "SUPABASE_DB_URL": "postgresql://example.invalid/database",
            "EMBEDDING_DIMENSION": "768",
            "EMBEDDING_PROVIDER_3072": "ollama",
            "EMBEDDING_MODEL_NAME_3072": "qwen3-embedding:8b",
            "EMBEDDING_OLLAMA_HOST": "http://localhost:11434",
        }
        output = io.StringIO()

        with (
            patch.dict(os.environ, values, clear=True),
            patch.object(module.psycopg2, "connect") as connect_mock,
            redirect_stdout(output),
        ):
            module.embed_database(dimension=3072, limit=100, dry_run=True)

        self.assertFalse(model_mock.called)
        self.assertFalse(connect_mock.called)
        self.assertIn("Provider: ollama", output.getvalue())
        self.assertIn("Model: qwen3-embedding:8b", output.getvalue())
        self.assertIn("Dimensions: 3072", output.getvalue())
        self.assertIn("Ollama host: http://localhost:11434", output.getvalue())
        self.assertIn("configured (hidden)", output.getvalue())

    def test_ollama_embedder_uses_requested_output_dimensions(self):
        module, _model_mock = load_embedding_module()
        recorded = {}

        class FakeClient:
            def __init__(self, host):
                recorded["host"] = host

            def embed(self, **kwargs):
                recorded.update(kwargs)
                return types.SimpleNamespace(embeddings=[[0.5] * 3072])

        fake_ollama = types.ModuleType("ollama")
        fake_ollama.Client = FakeClient
        settings = module.EmbeddingSettings(
            database_url="postgresql://example.invalid/database",
            provider="ollama",
            model_name="qwen3-embedding:8b",
            dimension=3072,
            device=None,
            normalize=False,
            connect_timeout=20,
            ollama_host="http://localhost:11434",
        )

        with patch.dict(sys.modules, {"ollama": fake_ollama}):
            vector = module._create_embedder(settings)("article text")

        self.assertEqual(len(vector), 3072)
        self.assertEqual(recorded["host"], "http://localhost:11434")
        self.assertEqual(recorded["model"], "qwen3-embedding:8b")
        self.assertEqual(recorded["input"], ["article text"])
        self.assertEqual(recorded["dimensions"], 3072)
        self.assertTrue(recorded["truncate"])

    def test_replace_incompatible_requires_confirmation(self):
        module, _model_mock = load_embedding_module()
        cursor = Mock()

        with self.assertRaisesRegex(ValueError, "confirm-replace"):
            module._replace_incompatible_embeddings(cursor, 3072, None)

        cursor.execute.assert_not_called()

    def test_replace_incompatible_clears_only_other_dimensions(self):
        module, _model_mock = load_embedding_module()
        cursor = Mock()
        cursor.rowcount = 652

        replaced = module._replace_incompatible_embeddings(
            cursor,
            3072,
            "REPLACE-EMBEDDINGS-3072",
        )

        self.assertEqual(replaced, 652)
        statement, parameters = cursor.execute.call_args.args
        self.assertIn('SET "embedding" = NULL', statement)
        self.assertIn('vector_dims("embedding") <> %s', statement)
        self.assertEqual(parameters, (3072,))

    def test_article_updates_use_article_id(self):
        content = EMBED_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('SELECT "article_id", "title", "abstract"', content)
        self.assertIn('WHERE "article_id" = %s', content)
        self.assertNotIn('SELECT "id", "title", "abstract"', content)

    def test_existing_embeddings_cannot_mix_dimensions(self):
        module, _model_mock = load_embedding_module()
        cursor = Mock()
        cursor.fetchall.return_value = [{"dimension": 768}]

        with self.assertRaisesRegex(ValueError, "refusing to mix"):
            module._validate_existing_dimensions(cursor, 3072)

    def test_pipeline_exposes_vector_embedding_option(self):
        content = RUN_PIPELINE.read_text(encoding="utf-8")

        self.assertIn("V. Embed Article Vectors", content)
        self.assertIn("if errorlevel 15 goto embed_db", content)
        self.assertIn(":embed_db", content)
        self.assertIn("--dry-run-embedding-menu", content)
        self.assertIn("--dimension %embedding_dimension%", content)
        self.assertIn("--dry-run-embedding", content)
        self.assertIn("--timeout 120 --retries 10", content)
        self.assertIn(
            "1. 768 dimensions - all-mpnet-base-v2 (Local)",
            content,
        )
        self.assertIn(
            "2. 3072 dimensions - qwen3-embedding:8b (Ollama Local)",
            content,
        )
        self.assertLess(
            content.index("1. 768 dimensions"),
            content.index("2. 3072 dimensions"),
        )
        self.assertNotIn("384 dimensions - all-MiniLM-L6-v2", content)


if __name__ == "__main__":
    unittest.main()
