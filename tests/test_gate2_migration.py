from __future__ import annotations

import re
import unittest
from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "20261106000100_gate2_windows_companion.sql"


class Gate2MigrationReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_private_tables_have_no_direct_client_grants(self):
        self.assertIn("REVOKE ALL ON SCHEMA gate2_private FROM PUBLIC, anon, authenticated, service_role", self.sql)
        self.assertIn("REVOKE ALL ON ALL TABLES IN SCHEMA gate2_private", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertNotIn("FORCE ROW LEVEL SECURITY", self.sql)  # security-definer owner must bypass RLS

    def test_all_public_rpc_functions_use_fixed_search_path_and_restricted_grants(self):
        public_functions = re.findall(
            r"CREATE OR REPLACE FUNCTION public\.(gate2_[a-z_]+).*?\$function\$;",
            self.sql,
            flags=re.S,
        )
        self.assertEqual(
            set(public_functions),
            {"gate2_ping", "gate2_claim_command", "gate2_publish_initial_snapshot", "gate2_publish_result"},
        )
        self.assertEqual(self.sql.count("SECURITY DEFINER\nSET search_path = ''"), 4)
        self.assertIn("FROM PUBLIC, anon, authenticated, service_role;", self.sql)
        self.assertEqual(self.sql.count("TO anon;"), 4)

    def test_pairing_token_is_read_from_header_and_session_id_is_derived(self):
        self.assertIn("request.headers", self.sql)
        self.assertIn("x-arena-gate2-token", self.sql)
        self.assertIn("pg_catalog.lower(header.key)", self.sql)
        self.assertIn("matching_header_count <> 1", self.sql)
        self.assertIn("_constant_time_equal(session_row.token_hash, candidate_hash)", self.sql)
        self.assertIn("FOR UPDATE OF session_row", self.sql)
        self.assertIn("_session_for_request()", self.sql)
        self.assertNotIn("p_session_id", self.sql)

    def test_result_table_does_not_store_marker_color(self):
        results_block = self.sql.split("CREATE TABLE gate2_private.results", 1)[1].split("CREATE INDEX gate2_results_session_idx", 1)[0]
        self.assertNotRegex(results_block, r"\b(marker_color|observed_color|color)\b")


if __name__ == "__main__":
    unittest.main()
