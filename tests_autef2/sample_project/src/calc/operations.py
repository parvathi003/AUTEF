"""A deliberately tiny module, used as a fixture for the AUTEF v2 tests."""


class Calculator:
    def add(self, a, b):
        return a + b

    def multiply(self, a, b):
        return a * b

    def divide(self, a, b):
        if b == 0:
            raise ValueError("division by zero")
        return a / b

    def describe(self):
        return "Calculator(precision=2)"
