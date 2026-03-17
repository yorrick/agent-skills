from src.mathlib import add, calculate_average, calculate_std_dev, subtract


def test_average():
    assert calculate_average([1, 2, 3]) == 2.0


def test_std_dev():
    result = calculate_std_dev([2, 4, 4, 4, 5, 5, 7, 9])
    assert abs(result - 2.0) < 0.1


def test_add():
    assert add(1, 2) == 3


def test_subtract():
    assert subtract(5, 3) == 2
