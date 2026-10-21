# Retry Mechanism Redesign Plan

## Objective
Implement a robust retry mechanism for operations in `autowsgr/ops` that involve user interaction (clicking) followed by state verification. The design must allow applying a decorator directly to the operation methods, avoiding nested function definitions.

## Design

### 1. `retry_action` Decorator (`autowsgr/ops/retry.py`)

The decorator will be enhanced to support:
-   **Exception Handling**: Retrying when specific exceptions are raised.
-   **No explicit validation callback**: The decorated function itself is expected to raise an exception if the operation fails (e.g., using `wait_leave_page` inside the function).

```python
# autowsgr/ops/retry.py

import functools
import time
from typing import Callable, TypeVar, Any, Sequence
from autowsgr.infra.logger import get_logger

_log = get_logger("ops.retry")

T = TypeVar("T")

def retry_action(
    retries: int = 1,
    delay: float = 1.0,
    exceptions: Sequence[type[Exception]] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            for i in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if i == retries:
                        _log.error(f"Operation {func.__name__} failed after {retries} retries: {e}")
                        raise e
                    _log.warning(f"Operation {func.__name__} failed: {e}. Retrying ({i+1}/{retries})...")
                    time.sleep(delay)
            return func(*args, **kwargs)
        return wrapper
    return decorator
```

### 2. Implementation Pattern

For each target operation, we will define a dedicated method (if not already present) that performs the atomic action: **Action + Verification**. This method will be decorated with `@retry_action`.

#### Pattern Example

```python
    @retry_action(retries=1, delay=1.0, exceptions=(NavigationError,))
    def _start_battle(self, page: BattlePreparationPage) -> None:
        """Atomic operation: Click start and verify page transition."""
        page.start_battle()
        # Verification raises NavigationError if it fails
        wait_leave_page(
            self._ctrl,
            checker=BaseBattlePreparation.is_current_page,
            timeout=2.0,
            source=PageName.BATTLE_PREP,
            target="combat"
        )
```

## Proposed Changes

### 1. `autowsgr/ops/normal_fight.py` (Normal Fight)
-   **Class**: `NormalFightRunner`
-   **New Method**: `_start_battle(self, page)` decorated with `@retry_action`.
-   **Usage**: Call `self._start_battle(page)` inside `_prepare_for_battle`.

### 2. `autowsgr/ops/exercise.py` (Exercise)
-   **Class**: `ExerciseRunner`
-   **New Method**: `_start_battle(self, page)` decorated with `@retry_action`.
-   **Usage**: Call `self._start_battle(page)` inside `_prepare_for_battle`.

### 3. `autowsgr/ops/event_fight.py` (Event Fight)
-   **Class**: `EventFightRunner`
-   **New Method**: `_start_battle(self, page)` decorated with `@retry_action`.
-   **Usage**: Call `self._start_battle(page)` inside `_prepare_for_battle`.

### 4. `autowsgr/ops/decisive/handlers.py` (Decisive Battle)
-   **Class**: `DecisivePhaseHandlers`
-   **New Method**: `_start_battle(self, page)` decorated with `@retry_action`.
-   **Usage**: Call `self._start_battle(page)` inside `_handle_prepare_combat`.

### 5. `autowsgr/ops/startup.py` (Game Startup)
-   **Function**: `_enter_game(ctrl)` (New helper function)
-   **Decorator**: Apply `@retry_action` to `_enter_game`.
-   **Usage**:
    ```python
    @retry_action(retries=1, delay=1.0, exceptions=(TimeoutError,))
    def _enter_game(ctrl: AndroidController) -> None:
        StartScreenPage(ctrl).click_enter()
        if not wait_for_game_ui(ctrl, timeout=30.0):
            raise TimeoutError("Timeout waiting for game UI")

    # In start_game:
    if StartScreenPage.is_current_page(ctrl.screenshot()):
        _enter_game(ctrl)
    ```
    *Note: Since `startup.py` uses standalone functions, we can define the helper inside `start_game` or at module level. Defining it at module level (or as a private helper) is cleaner.*

## Verification Strategy
-   The decorator relies on the wrapped function raising an exception.
-   `wait_leave_page` raises `NavigationError`.
-   `wait_for_game_ui` returns `bool`, so we must manually raise `TimeoutError` if it returns `False`.
