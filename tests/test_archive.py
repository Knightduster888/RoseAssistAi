"""
Hermetic unit tests for archive.py — designed for CI (GitHub Actions).

Runs entirely inside a temporary directory via ROSE_SHARED_DIR so it never
touches the live /root/shared-agents workspace, inbox, or archives.

    python3 tests/test_archive.py
    python3 -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        # Point archive.py at a throwaway dir for the duration of the test.
        self._tmp = tempfile.mkdtemp(prefix="rose_archive_test_")
        self._old_vars = os.environ.get("ROSE_SHARED_DIR")
        os.environ["ROSE_SHARED_DIR"] = self._tmp
        # Re-import so module constants pick up the override.
        import archive as ar
        self.archive = ar

    def tearDown(self):
        if self._old_vars is None:
            os.environ.pop("ROSE_SHARED_DIR", None)
        else:
            os.environ["ROSE_SHARED_DIR"] = self._old_vars
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_archive_roundtrip(self):
        msgs = [
            {"sender": "rose", "date": "2026-07-31",
             "subject": "Design brief",
             "body_md": "Apple-quality UI, restrained color."},
            {"sender": "alex", "date": "2026-07-31",
             "subject": "Design brief",
             "body_md": "Agreed, Inter + reduced-motion."},
        ]
        folder = self.archive.archive_session(msgs, heading="Shared workspace design")
        self.assertTrue(os.path.isdir(folder), "archive folder not created")
        self.assertTrue(os.path.isfile(os.path.join(folder, "SUMMARY.md")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "transcript.md")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "thread.json")))
        # Index should have exactly one entry
        entries = self.archive.list_archive()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["heading"], "Shared workspace design")
        self.assertEqual(entries[0]["message_count"], 2)
        # Inline-expandable thread survives JSON round-trip
        loaded = self.archive.load_thread(folder)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["sender"], "rose")

    def test_recall_finds_and_misses(self):
        msgs = [
            {"sender": "corey", "date": "2026-07-31",
             "subject": "Kickoff", "body_md": "Let's build the AppVantage board."},
        ]
        self.archive.archive_session(msgs, heading="Project kickoff")
        hits = self.archive.recall("AppVantage")
        self.assertEqual(len(hits), 1, "should find keyword in transcript")
        self.assertIn("AppVantage", hits[0]["snippet"])
        misses = self.archive.recall("obviously-not-here")
        self.assertEqual(misses, [])

    def test_derive_heading_skips_generic(self):
        msgs = [
            {"sender": "rose", "subject": "Re: direct communication",
             "body_md": "hi"},
            {"sender": "alex", "subject": "Actual plan",
             "body_md": "Here's the plan"},
        ]
        h = self.archive.derive_heading(msgs)
        self.assertEqual(h, "Actual plan")
        # Fallback path when everything is generic / empty
        empty = self.archive.derive_heading([])
        self.assertEqual(empty, "empty-session")


if __name__ == "__main__":
    unittest.main(verbosity=2)
