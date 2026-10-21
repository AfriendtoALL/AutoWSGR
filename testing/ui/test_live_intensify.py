import os
from pathlib import Path
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from autowsgr.ui.intensify_workflow import IntensifyWorkflowError, ShipStats
from autowsgr.ui.live_intensify import (
    FixedTargetOperator,
    GridSelectionRef,
    IntensifyHomePanelObservation,
    is_intensify_confirmation,
    is_target_selector,
    read_intensify_home_panel,
)
from autowsgr.vision.ocr import OCRResult


_ROOT = Path(os.environ.get('AUTOWSGR_LIVE_FIXTURE_ROOT', 'testing/fixtures/live-intensify'))


def _fixture(name: str) -> np.ndarray:
    path = _ROOT / name
    if not path.exists():
        pytest.skip(f'live intensify fixture unavailable: {path}')
    return cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)


def test_target_selector_predicate_excludes_material_selector() -> None:
    assert is_target_selector(_fixture('live-target-selector-current.png'))
    assert not is_target_selector(_fixture('live-explicit-material-open.png'))
    assert not is_target_selector(_fixture('cetus-16448-target-current.png'))


def test_confirmation_predicate_is_intensify_specific() -> None:
    assert is_intensify_confirmation(_fixture('live-intensify-confirm-dialog.png'))
    assert not is_intensify_confirmation(_fixture('live-explicit-preview.png'))


def test_grid_reference_round_trip_and_validation() -> None:
    expected = GridSelectionRef('scan-57', 12, 1, 3, 0.425, 0.565)
    assert GridSelectionRef.parse(expected.encode('material'), 'material') == expected
    with pytest.raises(IntensifyWorkflowError, match='格式错误'):
        GridSelectionRef.parse(expected.encode('target'), 'material')


def test_fixed_target_operator_injects_scroll_input(monkeypatch: pytest.MonkeyPatch) -> None:
    stepper_type = MagicMock()
    monkeypatch.setattr(
        'autowsgr.ui.live_target_inventory.CetusTargetScanDevice',
        stepper_type,
    )
    device = MagicMock()
    scroll_input = MagicMock()
    reader = MagicMock()

    FixedTargetOperator(device, 'revision', scroll_input, reader)

    stepper_type.assert_called_once_with(device, scroll_input=scroll_input)


def test_home_panel_reads_current_stats_and_preview_gains() -> None:
    ocr = MagicMock()
    ocr.recognize_batch.return_value = [
        [OCRResult('0', 0.99)],
        [OCRResult('+0', 0.99)],
        [OCRResult('', 0.0)],
        [OCRResult('', 0.0)],
        [OCRResult('9', 0.99)],
        [OCRResult('+1', 0.99)],
        [OCRResult('47', 0.99)],
        [OCRResult('+5', 0.99)],
    ]

    observation = read_intensify_home_panel(
        _fixture('live-home-material-preview.png'),
        ocr,
    )

    assert observation == IntensifyHomePanelObservation(
        current=ShipStats(firepower=0, torpedo=0, armor=9, anti_air=47),
        gains=ShipStats(firepower=0, torpedo=0, armor=1, anti_air=5),
        can_intensify=True,
    )
    ocr.recognize_batch.assert_called_once()


def test_home_panel_rejects_uncertain_available_stat() -> None:
    ocr = MagicMock()
    ocr.recognize_batch.return_value = [
        [OCRResult('0', 0.99)],
        [OCRResult('+0', 0.99)],
        [],
        [],
        [OCRResult('9', 0.20)],
        [OCRResult('+1', 0.99)],
        [OCRResult('47', 0.99)],
        [OCRResult('+5', 0.99)],
    ]

    with pytest.raises(IntensifyWorkflowError, match='主页属性面板'):
        read_intensify_home_panel(_fixture('live-home-material-preview.png'), ocr)


def test_home_panel_zero_gains_are_not_executable() -> None:
    ocr = MagicMock()
    ocr.recognize_batch.return_value = [
        [OCRResult('0', 0.99)],
        [OCRResult('+0', 0.99)],
        [],
        [],
        [OCRResult('9', 0.99)],
        [OCRResult('+0', 0.99)],
        [OCRResult('47', 0.99)],
        [OCRResult('+0', 0.99)],
    ]

    observation = read_intensify_home_panel(_fixture('live-home-after-clear.png'), ocr)

    assert observation.gains == ShipStats()
    assert not observation.can_intensify
