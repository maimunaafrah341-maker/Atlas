"""Import every model so Base.metadata is complete (Alembic, drift tests, the seed)."""

from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.shipping.models import ShippingRate, ShippingRateCountry

__all__ = [
    "InventoryMovement",
    "Product",
    "ProductTag",
    "ProductTranslation",
    "ShippingRate",
    "ShippingRateCountry",
    "Variant",
]
