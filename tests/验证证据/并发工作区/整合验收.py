"""维护者对演练项目的补充验收，参数为整合工作区的绝对路径。"""

import importlib.util
from pathlib import Path
import sys
import unittest


project = Path(sys.argv.pop(1)).resolve()
spec = importlib.util.spec_from_file_location("integrated_money", project / "money.py")
money = importlib.util.module_from_spec(spec)
spec.loader.exec_module(money)


class IntegratedAcceptance(unittest.TestCase):
    def test_subtotal_behavior(self):
        self.assertEqual(money.subtotal([]), 0)
        self.assertEqual(money.subtotal([(25, 2), (10, 3)]), 80)
        self.assertEqual(money.subtotal([(0, 3), (10, 0)]), 0)
        for items in ([(-1, 1)], [(1, -1)]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                money.subtotal(items)

    def test_discount_behavior(self):
        for amount, reduction, expected in ((100, 20, 80), (100, 100, 0), (10, 20, 0), (0, 0, 0)):
            with self.subTest(amount=amount, reduction=reduction):
                self.assertEqual(money.discounted_total(amount, reduction), expected)
        for args in ((-1, 0), (0, -1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                money.discounted_total(*args)

    def test_shipping_boundaries(self):
        for amount, expected in ((0, 10), (99, 10), (100, 0), (101, 0)):
            with self.subTest(amount=amount):
                self.assertEqual(money.shipping_fee(amount), expected)
        with self.assertRaises(ValueError):
            money.shipping_fee(-1)

    def test_user_input_is_preserved(self):
        self.assertEqual(money.CURRENCY, "CNY")
        self.assertEqual((project / "用户草稿.txt").read_text(), "用户未跟踪草稿，必须保留。\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
