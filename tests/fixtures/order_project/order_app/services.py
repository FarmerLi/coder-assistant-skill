from dataclasses import replace

from .domain import Order
from .store import MemoryOrderStore


class OrderService:
    def __init__(self, store: MemoryOrderStore):
        self._store = store

    def cancel(self, order_id: str, actor_id: str) -> Order:
        order = self._store.get(order_id)
        if order.owner != actor_id:
            raise PermissionError("只有订单所有者可以取消")
        if order.status == "cancelled":
            return order
        if order.status != "open":
            raise ValueError("当前订单状态不允许取消")
        updated = replace(order, status="cancelled")
        self._store.save(updated)
        return updated
