from .services import OrderService


class OrderAPI:
    def __init__(self, service: OrderService):
        self.service = service

    def cancel_order(self, order_id: str, actor_id: str) -> dict:
        raise NotImplementedError("取消入口尚未接入")
