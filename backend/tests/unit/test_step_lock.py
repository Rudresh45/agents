from __future__ import annotations

import pytest
from app.utils.step_lock import StepLock, StepLockBusyError


def test_step_lock_is_exclusive_and_reusable(tmp_path) -> None:
    with StepLock(tmp_path, "hr_manager.screen.apply"), pytest.raises(StepLockBusyError):
        StepLock(tmp_path, "hr_manager.screen.apply").acquire()

    # The lock is released on exit, so the next attempt can run.
    with StepLock(tmp_path, "hr_manager.screen.apply"):
        pass
