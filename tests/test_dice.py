"""The bounded dice slice, independent of Discord and the browser."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.dice import DiceRollError, parse_expression, roll_expression


class DiceExpressionTests(unittest.TestCase):
    def test_parser_normalizes_supported_expressions(self) -> None:
        self.assertEqual(parse_expression(" d20 ").normalized, "d20")
        self.assertEqual(parse_expression("2d6 + 3").normalized, "2d6+3")
        self.assertEqual(parse_expression("1d20-1").normalized, "d20-1")

    def test_parser_rejects_unbounded_or_ambiguous_input(self) -> None:
        for expression in ("d20*2", "0d6", "21d6", "1d101", "d20+101"):
            with self.subTest(expression=expression):
                with self.assertRaises(DiceRollError):
                    parse_expression(expression)

    def test_roll_returns_all_values_and_modifier(self) -> None:
        values = iter((2, 5))
        result = roll_expression("2d6+3", rng=lambda _: next(values))
        self.assertEqual(result["dice"], [{"sides": 6, "values": [2, 5]}])
        self.assertIsNone(result["selected"])
        self.assertEqual(result["modifier"], 3)
        self.assertEqual(result["total"], 10)

    def test_advantage_keeps_both_attempts_and_counts_the_higher_one(self) -> None:
        values = iter((4, 17))
        result = roll_expression("d20", mode="advantage", rng=lambda _: next(values))
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["natural"], 17)
        self.assertEqual(result["total"], 17)

    def test_advantage_is_only_for_one_d20(self) -> None:
        with self.assertRaisesRegex(DiceRollError, "one d20"):
            roll_expression("2d20", mode="advantage", rng=lambda _: 10)


if __name__ == "__main__":
    unittest.main()
