"""Module A docstring."""

class Calculator:
    """A simple calculator class."""

    def add(self, x: int, y: int) -> int:
        """Add two numbers."""
        # Add logic
        result = x + y
        return result

    def subtract(self, x: int, y: int) -> int:
        """Subtract two numbers."""
        # Subtract logic
        return x - y

def run_calculator():
    """Run calculator functions."""
    calc = Calculator()
    # Call add method
    r1 = calc.add(5, 3)
    # Call subtract method
    r2 = calc.subtract(10, 4)
    print(f"Result: {r1}, {r2}")
