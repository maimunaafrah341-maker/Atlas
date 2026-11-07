"""Import every model so Base.metadata is complete (Alembic, drift tests, the seed)."""

from app.domain.cart.models import Cart, CartItem
from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.reservations.models import Reservation, ReservationItem
from app.domain.shipping.models import ShippingRate, ShippingRateCountry

__all__ = [
    "Cart",
    "CartItem",
    "InventoryMovement",
    "Product",
    "ProductTag",
    "ProductTranslation",
    "Reservation",
    "ReservationItem",
    "ShippingRate",
    "ShippingRateCountry",
    "Variant",
]
