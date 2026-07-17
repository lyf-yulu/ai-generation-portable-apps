from enum import StrEnum

from pydantic import BaseModel


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration_error"
    PERMISSION = "permission_error"
    DOCUMENT = "document_error"
    VALIDATION = "validation_error"
    TRANSIENT = "transient_error"
    PROVIDER_TERMINAL = "provider_terminal_error"
    DELIVERY = "delivery_error"


class ErrorDetail(BaseModel):
    category: ErrorCategory
    message: str
    technical_detail: str
    retryable: bool


class AgentError(RuntimeError):
    def __init__(self, detail: ErrorDetail):
        super().__init__(detail.message)
        self.detail = detail
