"""Natural-language product discovery (M5 MVP, ADR-005, ADR-028).

The model only turns a shopper's words into a ``ShoppingIntent`` made of closed catalogue codes
and a few words copied from the request. The backend validates it, runs the ordinary catalogue
search and returns products from the database. Without the model, a deterministic keyword
parser produces the same intent shape.
"""
