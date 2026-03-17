from src.mathlib import calculate_average


def test_average_normal():
    assert calculate_average([1, 2, 3]) == 2.0


def test_average_empty():
    assert calculate_average([]) == 0.0
