"""API 错误：统一成 {"error": {"code", "message", "detail"}}。"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400,
                 detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return {"error": out}


def not_found(what: str, ident: Any) -> ApiError:
    return ApiError(f"{what.upper()}_NOT_FOUND", f"{what} {ident} 不存在", 404)
