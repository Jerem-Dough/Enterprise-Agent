"""One place for the failures the harness is expected to produce.

These are not bugs. Each one is an outcome the audit log should be able to
explain, so each carries enough structure to be written down rather than just
printed.
"""


class SiloError(Exception):
    """Base for every failure the harness raises on purpose."""

    def as_dict(self) -> dict:
        return {"error": type(self).__name__, "message": str(self)}


class ScopeDenied(SiloError):
    """A read or write was attempted without the principal's scope.

    Raised by the store, not by the caller's own check, so that forgetting to
    check is not the same as being allowed.
    """

    def __init__(self, principal_id: str, required: str, operation: str) -> None:
        super().__init__(
            f"{principal_id} lacks scope {required!r} required for {operation}"
        )
        self.principal_id = principal_id
        self.required = required
        self.operation = operation

    def as_dict(self) -> dict:
        return super().as_dict() | {
            "principal": self.principal_id,
            "required_scope": self.required,
            "operation": self.operation,
        }


class PolicyViolation(SiloError):
    """A plan was well formed and permitted, and policy still refused it."""

    def __init__(self, rule: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.rule = rule
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return super().as_dict() | {"rule": self.rule, "detail": self.detail}


class ApprovalRequired(SiloError):
    """Execution stopped because no human has said yes yet."""

    def __init__(self, approval_id: str, approver_id: str) -> None:
        super().__init__(f"approval {approval_id} is pending with {approver_id}")
        self.approval_id = approval_id
        self.approver_id = approver_id


class StepFailed(SiloError):
    """A workflow step failed. The engine decides whether to compensate."""

    def __init__(self, step_id: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"step {step_id}: {message}")
        self.step_id = step_id
        self.retryable = retryable


class AuditTampered(SiloError):
    """The audit hash chain did not verify."""
