import random
import unittest

from schedule import SpareCapacity, distribute


class SpareCapacityTests(unittest.TestCase):
    def test_nonempty_worker_can_include_zero_cost_items(self):
        fill = SpareCapacity({0: 0, 1: 12, 2: 3000})
        self.assertEqual(fill.fill([[1]]), [[1, 0]])
        self.assertEqual(fill.fill([]), [])

    def test_padding_preserves_candidates_and_price(self):
        rng = random.Random(3168)
        for _ in range(250):
            millis = {n: rng.choice((0, 1, 2, 1000, 14000, 18000)) for n in range(25)}
            chosen = rng.sample(list(millis), rng.randrange(26))
            original = distribute(chosen, millis)
            fill = SpareCapacity(millis)
            padded = fill.fill(original)
            items = [n for group in padded for n in group]
            self.assertEqual(len(items), len(set(items)))
            self.assertLessEqual(set(chosen), set(items))
            self.assertLessEqual(set(items), millis.keys())
            self.assertLessEqual(fill.cost(padded), fill.cost(original))
            self.assertTrue(all(padded))
            self.assertLessEqual(len(padded), 8)
