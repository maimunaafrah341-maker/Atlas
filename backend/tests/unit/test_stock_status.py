import pytest

from app.domain.inventory.service import is_low_stock, stock_status


@pytest.mark.parametrize(
    ("available", "threshold", "expected"),
    [
        (5, 5, True),  # D-6: at the threshold counts as low
        (4, 5, True),
        (6, 5, False),
        (1, 0, False),  # threshold 0 disables low-stock
        (0, 0, False),
    ],
)
def test_is_low_stock(available: int, threshold: int, expected: bool) -> None:
    assert is_low_stock(available, threshold) is expected


@pytest.mark.parametrize(
    ("available", "threshold", "expected"),
    [
        (0, 3, "out_of_stock"),
        (0, 0, "out_of_stock"),
        (3, 3, "low_stock"),
        (4, 3, "in_stock"),
        (1, 0, "in_stock"),
    ],
)
def test_stock_status(available: int, threshold: int, expected: str) -> None:
    assert stock_status(available, threshold) == expected
