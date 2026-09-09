"""The fixture project's own suite.

Two of these pass and two are deliberately broken: one carries a wrong
expectation, the other calls a method that does not exist. They are the input
the AUTEF tests run the pipeline over rather than tests of AUTEF itself, which
is why ``conftest.py`` tells pytest to ignore this directory.

Both shapes are here on purpose. ``unittest.TestCase`` methods and a plain
pytest function exercise the two ways a project can write tests, and the
resolver has to locate a failing function in either.
"""

import pytest

import unittest

from calc.operations import Calculator


class TestCalculator(unittest.TestCase):
    def setUp(self):
        self.calc = Calculator()

    def test_add_returns_sum(self):
        self.assertEqual(self.calc.add(3, 4), 7)

    def test_multiply_returns_product(self):
        # Wrong expectation: 3 * 4 is 12.
        self.assertEqual(self.calc.multiply(3, 4), 14)

    def test_describe_uses_precision(self):
        # No such method on Calculator; the method is describe().
        self.assertEqual(self.calc.description(), "Calculator(precision=2)")


def test_divide_by_zero_raises():
    calc = Calculator()
    with pytest.raises(ValueError):
        calc.divide(1, 0)
