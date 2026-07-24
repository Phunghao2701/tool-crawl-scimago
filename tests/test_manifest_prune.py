import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = ROOT / "tools" / "report_manifest_prune.py"
MIGRATE_SCRIPT = ROOT / "tools" / "migrate_local_to_vercel.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location(
        "report_manifest_prune_test",
        REPORT_SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ManifestPruneTest(unittest.TestCase):
    def test_report_tool_contains_no_production_delete_or_truncate(self):
        source = REPORT_SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("delete from public.", source)
        self.assertNotIn("truncate table", source)
        self.assertNotIn("postgresql://", source)

    def test_dependency_closure_preserves_project_references(self):
        module = load_report_module()
        source = REPORT_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('"Project_Article_Bookmark"', source)
        self.assertIn('"Project_Journal"', source)
        self.assertIn('"Project_Keyword"', source)
        self.assertEqual(
            module.KEEP_TABLES["Author_Article"],
            "temp_keep_article",
        )
        self.assertEqual(
            module.KEEP_TABLES["Journal_Ranking"],
            "temp_keep_journal",
        )

    def test_general_migration_is_guarded_by_active_manifest(self):
        source = MIGRATE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("enforce_article_manifest_guard(vercel_engine)", source)
        self.assertIn(
            "Generic local -> production migration is locked",
            source,
        )

    def test_guard_allows_migration_without_manifest(self):
        import migrate_local_to_vercel as migration

        engine = Mock()
        with unittest.mock.patch.object(
            migration,
            "get_active_article_manifest",
            return_value=None,
        ):
            migration.enforce_article_manifest_guard(engine)

    def test_guard_blocks_migration_with_manifest(self):
        import migrate_local_to_vercel as migration

        engine = Mock()
        with unittest.mock.patch.object(
            migration,
            "get_active_article_manifest",
            return_value={
                "manifest_name": "balanced-20k-v1",
                "selected_count": 20_000,
                "selection_checksum": "checksum",
            },
        ):
            with self.assertRaises(SystemExit):
                migration.enforce_article_manifest_guard(engine)


if __name__ == "__main__":
    unittest.main()
