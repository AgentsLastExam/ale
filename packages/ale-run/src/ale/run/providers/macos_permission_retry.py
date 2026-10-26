"""Bounded whole-flow retries. Recovery never resets granted permissions."""


def run_with_retries(flow, recover, retries=3):
    if not 0 <= retries <= 3:
        raise ValueError("retries must be between 0 and 3")
    attempts = []
    for number in range(1, retries + 2):
        phase = "recovery" if number > 1 else "flow"
        try:
            if number > 1:
                recover()
            phase = "flow"
            permissions = flow()
        except Exception as exc:
            attempts.append(
                {
                    "attempt": number,
                    "status": "failed",
                    "phase": phase,
                    "error_type": type(exc).__name__,
                }
            )
        else:
            attempts.append({"attempt": number, "status": "ui_enabled"})
            return {
                "status": "ui_enabled",
                "permissions": permissions,
                "attempts": attempts,
            }
    return {"status": "failed", "permissions": [], "attempts": attempts}
