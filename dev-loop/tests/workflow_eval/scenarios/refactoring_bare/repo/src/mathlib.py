import math


def calculate_average(numbers: list[float]) -> float:
    """Return the average of a list of numbers."""
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)


def calculate_std_dev(numbers: list[float]) -> float:
    """Return the standard deviation of a list of numbers."""
    if len(numbers) < 2:
        return 0.0
    avg = calculate_average(numbers)
    variance = sum((x - avg) ** 2 for x in numbers) / (len(numbers) - 1)
    return math.sqrt(variance)


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def multiply(a: float, b: float) -> float:
    return a * b
