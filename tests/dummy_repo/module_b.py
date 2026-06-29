"""Module B docstring."""
from tests.dummy_repo.module_a import Calculator, run_calculator

def calculate_average(x: int, y: int) -> float:
    """Calculate the average of two numbers using Calculator."""
    calc = Calculator()
    # Sum them
    total = calc.add(x, y)
    # Return half
    return total / 2.0

def main():
    """Main entrypoint."""
    # Run calculator
    run_calculator()
    avg = calculate_average(4, 6)
    print(f"Average: {avg}")
