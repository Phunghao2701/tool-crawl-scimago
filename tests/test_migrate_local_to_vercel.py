import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import migrate_local_to_vercel as migration


class FakeTransaction:
    def __init__(self):
        self.is_active = True
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True
        self.is_active = False

    def rollback(self):
        self.rolled_back = True
        self.is_active = False


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeDriverConnection:
    def cursor(self):
        return FakeCursor()


class FakeConnection:
    def __init__(self):
        self.transaction = FakeTransaction()
        self.connection = type(
            "ConnectionProxy",
            (),
            {"driver_connection": FakeDriverConnection()},
        )()
        self.invalidated = False
        self.closed = False

    def begin(self):
        return self.transaction

    def exec_driver_sql(self, _sql):
        return None

    def invalidate(self):
        self.invalidated = True

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self):
        self.connections = []

    def connect(self):
        conn = FakeConnection()
        self.connections.append(conn)
        return conn


class MigrationHelpersTest(unittest.TestCase):
    def test_composite_keyset_query_uses_all_pk_columns(self):
        query, params = migration.build_page_query(
            "Author_Article",
            ["author_id", "article_id", "author_position"],
            ["author_id", "article_id"],
            cursor=(10, 20),
        )
        self.assertIn(
            'WHERE ("author_id", "article_id") > (:cursor_0, :cursor_1)',
            query,
        )
        self.assertIn('ORDER BY "author_id", "article_id"', query)
        self.assertNotIn("OFFSET", query)
        self.assertEqual(params, {"cursor_0": 10, "cursor_1": 20})

    def test_initial_identity_resume_uses_one_offset(self):
        query, params = migration.build_page_query(
            "Article",
            ["article_id", "title"],
            ["article_id"],
            initial_offset=100,
        )
        self.assertIn('ORDER BY "article_id"', query)
        self.assertIn("OFFSET :off", query)
        self.assertEqual(params, {"off": 100})

    def test_prepare_values_serializes_json(self):
        values = migration.prepare_values(
            [(1, {"a": [1, 2]}), (2, None)],
            ["id", "payload"],
            {"payload"},
        )
        self.assertEqual(values[0][1], '{"a": [1, 2]}')
        self.assertIsNone(values[1][1])

    def test_journal_skips_unchanged_rows(self):
        cols = [
            "journal_id",
            "source_id",
            "display_name",
            "issn",
            "publisher_id",
            "country",
            "region",
            "created_at",
            "is_deleted",
        ]
        unchanged = (1, "S1", "Name", "1234", 9, "VN", "Asia", None, False)
        changed = (2, "S2-new", "Name 2", "5678", 10, "US", "NA", None, False)
        remote = {
            1: unchanged[1:],
            2: ("S2-old",) + changed[2:],
        }
        filtered, skipped = migration.filter_unchanged_upserts(
            "Journal",
            cols,
            [unchanged, changed],
            remote,
        )
        self.assertEqual(filtered, [changed])
        self.assertEqual(skipped, 1)

    def test_journal_upsert_only_updates_distinct_rows(self):
        sql = migration.build_values_insert_sql(
            "Journal",
            ["journal_id", "source_id", "display_name"],
        )
        self.assertIn("VALUES %s", sql)
        self.assertIn("IS DISTINCT FROM", sql)

    def test_transient_connection_error_retries_idempotent_batch(self):
        engine = FakeEngine()
        transient = migration.PsycopgOperationalError("connection closed")
        with patch.object(
            migration,
            "execute_values",
            side_effect=[transient, None],
        ) as execute_mock, patch.object(migration.time, "sleep") as sleep_mock:
            migration.execute_values_batch(
                engine,
                'INSERT INTO "T" ("id") VALUES %s ON CONFLICT DO NOTHING',
                [(1,), (2,)],
                disable_fk_checks=True,
            )

        self.assertEqual(execute_mock.call_count, 2)
        self.assertEqual(len(engine.connections), 2)
        self.assertTrue(engine.connections[0].invalidated)
        self.assertTrue(engine.connections[1].transaction.committed)
        sleep_mock.assert_called_once_with(migration.RETRY_BASE_SECONDS)


if __name__ == "__main__":
    unittest.main()
