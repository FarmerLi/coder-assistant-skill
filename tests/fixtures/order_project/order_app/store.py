from .domain import Order


class MemoryOrderStore:
    def __init__(self, orders):
        self._orders = {order.id: order for order in orders}
        self.save_count = 0

    def get(self, order_id: str) -> Order:
        return self._orders[order_id]

    def save(self, order: Order) -> None:
        self._orders[order.id] = order
        self.save_count += 1
