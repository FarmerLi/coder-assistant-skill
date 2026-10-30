from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_app.domain import Order
from order_app.services import OrderService
from order_app.store import MemoryOrderStore


class OrderServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryOrderStore([Order("A1", "alice"), Order("S1", "alice", "shipped")])
        self.service = OrderService(self.store)

    def test_owner_cancels_open_order(self):
        self.assertEqual(self.service.cancel("A1", "alice").status, "cancelled")
        self.assertEqual(self.store.get("A1").status, "cancelled")
        self.assertEqual(self.store.save_count, 1)

    def test_repeat_cancel_does_not_write_twice(self):
        self.service.cancel("A1", "alice")
        self.service.cancel("A1", "alice")
        self.assertEqual(self.store.save_count, 1)

    def test_stranger_is_rejected_without_write(self):
        with self.assertRaises(PermissionError):
            self.service.cancel("A1", "bob")
        self.assertEqual(self.store.get("A1").status, "open")
        self.assertEqual(self.store.save_count, 0)

    def test_shipped_order_is_rejected_without_write(self):
        with self.assertRaises(ValueError):
            self.service.cancel("S1", "alice")
        self.assertEqual(self.store.get("S1").status, "shipped")
        self.assertEqual(self.store.save_count, 0)

    def test_missing_order_is_rejected_without_write(self):
        with self.assertRaises(KeyError):
            self.service.cancel("missing", "alice")
        self.assertEqual(self.store.save_count, 0)

    def test_order_id_remains_case_sensitive(self):
        with self.assertRaises(KeyError):
            self.service.cancel("a1", "alice")
        self.assertEqual(self.store.save_count, 0)


if __name__ == "__main__":
    unittest.main()
