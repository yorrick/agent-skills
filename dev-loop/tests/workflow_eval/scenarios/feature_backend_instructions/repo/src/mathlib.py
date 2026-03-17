def calculate_average(numbers: list[float]) -> float:
    """Return the average of a list of numbers."""
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)
