import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tasklist import store  # noqa: E402


class StoreTest(unittest.TestCase):
    def test_add_and_mark_done(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tasks.json")
            first = store.add("Buy milk", path)
            self.assertEqual(first["id"], 1)
            self.assertTrue(store.mark_done(1, path)["done"])
            self.assertIsNone(store.mark_done(7, path))


if __name__ == "__main__":
    unittest.main()
