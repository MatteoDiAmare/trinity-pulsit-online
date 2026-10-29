"""Paper-trading-only adapter. No live order execution."""
class PaperTradingAdapter:
    def __init__(self):
        self.orders = []

    def submit(self, symbol, side, quantity):
        if side not in ("buy", "sell") or quantity <= 0:
            raise ValueError("invalid paper order")
        order = {"symbol": symbol, "side": side, "quantity": quantity, "mode": "paper"}
        self.orders.append(order)
        return order
