import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "build_article_manifest.py"
DDL = ROOT / "database" / "article_manifest.sql"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "build_article_manifest_test",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ArticleManifestTest(unittest.TestCase):
    def test_selection_is_balanced_and_preserves_bookmarks(self):
        module = load_module()
        selection = module.SELECTION_SQL

        self.assertIn('"Project_Article_Bookmark"', selection)
        self.assertIn("PARTITION BY f.primary_topic", selection)
        self.assertIn(
            "PARTITION BY COALESCE(r.subject_area_id, -1)",
            selection,
        )
        self.assertIn("'bookmarked'", selection)
        self.assertIn("'topic_representative'", selection)
        self.assertIn("'subject_area_balanced'", selection)
        self.assertIn("WHERE selected_rank <= %(target_count)s", selection)

    def test_tool_does_not_delete_article_data(self):
        source = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn('delete from public.\"article\"', source)
        self.assertNotIn('truncate table \"article\"', source)
        self.assertNotIn("postgresql://", source)

    def test_manifest_schema_is_private_and_versioned(self):
        ddl = DDL.read_text(encoding="utf-8")

        self.assertIn("CREATE SCHEMA IF NOT EXISTS pipeline", ddl)
        self.assertIn("REVOKE ALL ON SCHEMA pipeline FROM PUBLIC", ddl)
        self.assertIn("pipeline.article_manifests", ddl)
        self.assertIn("pipeline.article_manifest_items", ddl)
        self.assertIn("ON DELETE RESTRICT", ddl)
        self.assertIn("article_manifests_one_active_idx", ddl)

    def test_replace_requires_apply(self):
        module = load_module()

        with self.assertRaisesRegex(ValueError, "--replace"):
            module.build_manifest(
                target_count=20_000,
                manifest_name="balanced-20k-v1",
                apply=False,
                replace=True,
            )

    def test_manifest_name_is_restricted(self):
        module = load_module()

        self.assertEqual(
            module._validate_manifest_name("balanced-20k-v1"),
            "balanced-20k-v1",
        )
        with self.assertRaises(Exception):
            module._validate_manifest_name("unsafe manifest; drop table")


if __name__ == "__main__":
    unittest.main()
