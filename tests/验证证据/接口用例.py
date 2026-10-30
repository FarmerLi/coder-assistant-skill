from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_app.api import OrderAPI
from order_app.domain import Order
from order_app.services import OrderService
from order_app.store import MemoryOrderStore


class OrderAPITests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryOrderStore([Order("A1", "alice"), Order("S1", "alice", "shipped")])
        self.api = OrderAPI(OrderService(self.store))

    def test_owner_cancels_open_order(self):
        self.assertEqual(self.api.cancel_order("A1", "alice"), {"id": "A1", "status": "cancelled"})
        self.assertEqual(self.store.get("A1").status, "cancelled")
        self.assertEqual(self.store.save_count, 1)

    def test_repeat_cancel_does_not_write_twice(self):
        first = self.api.cancel_order("A1", "alice")
        self.assertEqual(self.api.cancel_order("A1", "alice"), first)
        self.assertEqual(self.store.save_count, 1)

    def test_stranger_is_rejected_without_change(self):
        with self.assertRaises(PermissionError):
            self.api.cancel_order("A1", "bob")
        self.assertEqual(self.store.get("A1").status, "open")
        self.assertEqual(self.store.save_count, 0)

    def test_shipped_order_is_rejected_without_change(self):
        with self.assertRaises(ValueError):
            self.api.cancel_order("S1", "alice")
        self.assertEqual(self.store.get("S1").status, "shipped")
        self.assertEqual(self.store.save_count, 0)

    def test_missing_order_is_rejected_without_write(self):
        with self.assertRaises(KeyError):
            self.api.cancel_order("missing", "alice")
        self.assertEqual(self.store.save_count, 0)

    def test_order_id_is_case_sensitive(self):
        with self.assertRaises(KeyError):
            self.api.cancel_order("a1", "alice")
        self.assertEqual(self.store.save_count, 0)


if __name__ == "__main__":
    unittest.main()
