import copy
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agentforge.codex_client import DeadlineExceeded
from agentforge.resource_profiles import ResourceProfiles
from agentforge.resources import read_json, write_json
from architecture_fixtures import fixture


class ResourceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = fixture()[1]["datasets"][0]
        self.cache = ResourceProfiles(self.root)

    def test_profile_counts_and_isolation_from_returned_values(self):
        result = self.cache.get(self.data)
        self.assertEqual(result["row_count"], 5)
        self.assertEqual(result["columns"]["amount"], {"blank_count": 1, "numeric_count": 4, "non_numeric_count": 0})
        self.assertEqual(result["columns"]["status"]["non_numeric_count"], 5)
        self.assertEqual(self.cache.last_audit["cache_status"], "miss")
        result["row_count"] = 999
        self.assertEqual(self.cache.get(self.data)["row_count"], 5)
        self.assertEqual(self.cache.last_audit["cache_status"], "memory_hit")

    def test_new_instance_reads_disk_without_recomputing(self):
        expected = self.cache.get(self.data)
        second = ResourceProfiles(self.root)
        with patch("agentforge.resource_profiles.number", side_effect=AssertionError("must use persisted profile")):
            self.assertEqual(second.get(self.data), expected)
        self.assertEqual(second.last_audit["cache_status"], "disk_hit")

    def test_data_algorithm_and_contract_version_invalidate_cache(self):
        self.cache.get(self.data)
        first = self.cache.last_audit["cache_key"]
        changed = copy.deepcopy(self.data)
        changed["rows"][0]["amount"] = None
        self.assertEqual(self.cache.get(changed)["columns"]["amount"]["blank_count"], 2)
        self.assertNotEqual(self.cache.last_audit["cache_key"], first)
        self.cache.algorithm_hash = "new-algorithm"
        self.cache.get(self.data)
        second = self.cache.last_audit["cache_key"]
        self.assertNotEqual(second, first)
        with patch("agentforge.resource_profiles.CONTRACT_VERSION", "future-contract"):
            self.cache.get(self.data)
        self.assertNotEqual(self.cache.last_audit["cache_key"], second)
        self.assertEqual(self.cache.last_audit["cache_status"], "miss")

    def test_corrupt_content_and_hash_recompute(self):
        expected = self.cache.get(self.data)
        path = self.cache.directory / (self.cache.last_audit["cache_key"] + ".json")
        saved = read_json(path)
        saved["result"]["row_count"] = 999
        write_json(path, saved)
        cache = ResourceProfiles(self.root)
        self.assertEqual(cache.get(self.data), expected)
        self.assertEqual(cache.last_audit["cache_status"], "invalidated")
        path.write_text("broken-json", encoding="utf-8")
        cache = ResourceProfiles(self.root)
        self.assertEqual(cache.get(self.data), expected)
        self.assertEqual(cache.last_audit["cache_status"], "unreadable")

    def test_write_failure_is_nonfatal_and_audited(self):
        with patch("agentforge.resource_profiles.write_json", side_effect=OSError("readonly")):
            self.assertEqual(self.cache.get(self.data)["row_count"], 5)
        self.assertTrue(self.cache.last_audit["cache_write_failed"])
        self.assertEqual(self.cache.get(self.data)["row_count"], 5)

    def test_concurrent_requests_have_one_computation_and_thread_local_audits(self):
        def get():
            result = self.cache.get(self.data)
            return result, copy.deepcopy(self.cache.last_audit)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: get(), range(8)))
        self.assertEqual(sum(a["cache_status"] == "miss" for _, a in results), 1)
        self.assertEqual(sum(a["cache_status"] == "memory_hit" for _, a in results), 7)
        self.assertTrue(all(r == results[0][0] for r, _ in results))
        self.assertIsNone(self.cache.last_audit)

    def test_expired_deadline_blocks_even_memory_hits_and_lock_waits(self):
        self.cache.get(self.data)
        with self.assertRaises(DeadlineExceeded):
            self.cache.get(self.data, deadline=time.monotonic() - 1)
        with self.cache._lock:
            with self.assertRaises(DeadlineExceeded):
                self.cache.get(self.data, deadline=time.monotonic() + 0.01, deadline_scope="build")

    def test_cache_is_scoped_to_resource_snapshot_not_only_dataset_id(self):
        self.cache.get(self.data)
        before = self.cache.last_audit["cache_key"]
        changed = copy.deepcopy(self.data)
        changed["data_contract"]["description"] = "different frozen contract"
        self.cache.get(changed)
        self.assertNotEqual(before, self.cache.last_audit["cache_key"])
