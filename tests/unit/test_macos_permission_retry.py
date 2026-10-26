from unittest.mock import Mock

import pytest

from ale.run.providers.macos_permission_retry import run_with_retries


def test_failure_restarts_whole_flow_and_keeps_successful_state():
    calls, granted = [], set()

    def flow():
        for permission in ("accessibility", "screen_recording"):
            calls.append(permission)
            if permission == "screen_recording" and len(calls) == 2:
                raise TimeoutError("temporary UI delay")
            granted.add(permission)
        return sorted(granted)

    recover = Mock()
    result = run_with_retries(flow, recover)
    assert result["status"] == "ui_enabled"
    assert calls == ["accessibility", "screen_recording"] * 2
    assert result["permissions"] == ["accessibility", "screen_recording"]
    assert len(result["attempts"]) == 2
    recover.assert_called_once_with()


def test_initial_attempt_plus_three_retries_and_no_diagnostic_secret_leak():
    flow = Mock(side_effect=TimeoutError("secret-vnc-password"))
    recover = Mock()
    result = run_with_retries(flow, recover)
    assert result["status"] == "failed"
    assert flow.call_count == 4
    assert recover.call_count == 3
    assert len(result["attempts"]) == 4
    assert "secret-vnc-password" not in str(result)


def test_recovery_failure_also_consumes_attempt_budget():
    flow = Mock(side_effect=TimeoutError())
    recover = Mock(side_effect=ConnectionError())
    result = run_with_retries(flow, recover)
    assert len(result["attempts"]) == 4
    assert flow.call_count == 1
    assert recover.call_count == 3
    assert result["attempts"][-1]["phase"] == "recovery"


def test_success_does_not_restart_settings():
    recover = Mock()
    result = run_with_retries(list, recover)
    assert result["status"] == "ui_enabled"
    assert len(result["attempts"]) == 1
    recover.assert_not_called()


def test_retry_count_is_bounded():
    for retries in (-1, 4):
        with pytest.raises(ValueError):
            run_with_retries(list, lambda: None, retries)
