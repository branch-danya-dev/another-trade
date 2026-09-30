from __future__ import annotations


class BybitError(RuntimeError):
    pass


class BybitHttpError(BybitError):
    def __init__(self, status_code: int, body: bytes) -> None:
        super().__init__(f"Bybit HTTP error {status_code}: {body[:300]!r}")
        self.status_code = status_code
        self.body = body


class BybitApiError(BybitError):
    def __init__(self, ret_code: int, ret_msg: str) -> None:
        super().__init__(f"Bybit retCode={ret_code}: {ret_msg}")
        self.ret_code = ret_code
        self.ret_msg = ret_msg


class BybitRateLimitError(BybitApiError):
    pass


class InventoryConflictError(BybitError):
    pass
