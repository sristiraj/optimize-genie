"""Per-request configuration extracted from setup wizard headers.

These values are set in OBOAuthMiddleware from x-databricks-host,
x-gso-warehouse-id, x-gso-catalog, and x-llm-model headers that the
frontend injects from localStorage when the user has completed setup.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class RequestConfig:
    host: str = ""
    warehouse_id: str = ""
    gso_catalog: str = ""
    llm_model: str = ""


_request_config: ContextVar[RequestConfig] = ContextVar(
    "_request_config", default=RequestConfig()
)


def get_request_config() -> RequestConfig:
    return _request_config.get()


def set_request_config(cfg: RequestConfig) -> None:
    _request_config.set(cfg)
