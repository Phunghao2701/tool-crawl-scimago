import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
SCRIPT = TOOLS / "apply_manifest_prune.py"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "apply_manifest_prune_test",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ApplyManifestPruneTest(unittest.TestCase):
    def test_child_first_order(self):
        module = load_module()
        stages = module.PRUNE_STAGES

        self.assertLess(stages.index("Author_Article"), stages.index("Article"))
        self.assertLess(stages.index("Keyword_Article"), stages.index("Article"))
        self.assertLess(stages.index("Sub_Topic"), stages.index("Article"))
        self.assertLess(stages.index("Article"), stages.index("Issue"))
        self.assertLess(stages.index("Issue"), stages.index("Volume"))
        self.assertLess(stages.index("Volume"), stages.index("Journal"))
        self.assertLess(
            stages.index("Journal_Ranking_Subject_Category"),
            stages.index("Journal_Ranking"),
        )

    def test_apply_requires_backup_and_confirmation(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("--backup", source)
        self.assertIn("--confirm", source)
        self.assertIn("PRUNE-{manifest['manifest_name']}", source)
        self.assertIn("pipeline.article_prune_runs", source)

    def test_delete_is_batched_through_temp_queue(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("DELETE FROM temp_prune_queue", source)
        self.assertIn("LIMIT %s", source)
        self.assertIn("DELETE FROM public.{}", source)
        self.assertIn("_update_checkpoint", source)
        self.assertIn("source.ctid AS source_ctid", source)
        self.assertIn("source.ctid = batch.source_ctid", source)
        self.assertIn("idx_jrsc_journal_ranking_id", source)
        self.assertIn("idx_keyword_article_article_id", source)

    def test_no_truncate_or_cascade(self):
        source = SCRIPT.read_text(encoding="utf-8").lower()

        self.assertNotIn("truncate table", source)
        self.assertNotIn("delete cascade", source)
        self.assertNotIn("postgresql://", source)


if __name__ == "__main__":
    unittest.main()
