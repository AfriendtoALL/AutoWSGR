from __future__ import annotations

import json
from typing import TYPE_CHECKING

import cv2
import numpy as np
import pytest

from autowsgr.vision.named_portrait_matcher import NamedPortraitMatcher


if TYPE_CHECKING:
    from pathlib import Path


def _portrait(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((180, 120, 3), 235, dtype=np.uint8)
    for _ in range(24):
        center = tuple(int(value) for value in rng.integers((5, 5), (115, 175)))
        cv2.circle(image, center, int(rng.integers(2, 10)), (20, 80, 160), -1)
    return image


def test_named_matcher_only_compares_ocr_constrained_forms(tmp_path: Path) -> None:
    assets = tmp_path / 'assets'
    assets.mkdir()
    target = _portrait(1)
    other_form = _portrait(2)
    unrelated = target.copy()
    ships = []
    for ship_id, name, image in (
        (1, '约克', target),
        (2, '约克', other_form),
        (3, '无关舰', unrelated),
    ):
        path = assets / f'{ship_id}.png'
        cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        ships.append(
            {
                'id': ship_id,
                'name': name,
                'search_name': name,
                'ship_type': 'ca',
                'portrait': f'assets/{ship_id}.png',
            }
        )
    (tmp_path / 'manifest.json').write_text(json.dumps({'ships': ships}), encoding='utf-8')

    matcher = NamedPortraitMatcher(tmp_path)
    match = matcher.identify(target, '约克')

    assert match is not None
    assert match.record.ship_id == 1
    assert matcher.identify(target, '不存在') is None
    assert matcher.search_names == ['约克', '无关舰']


def test_named_matcher_rejects_malformed_manifest_entry(tmp_path: Path) -> None:
    (tmp_path / 'manifest.json').write_text(
        json.dumps({'ships': [{'id': 1, 'name': '缺头像', 'ship_type': 'ca'}]}),
        encoding='utf-8',
    )

    with pytest.raises(ValueError, match='条目无效'):
        NamedPortraitMatcher(tmp_path)


def test_named_matcher_rejects_missing_portrait(tmp_path: Path) -> None:
    (tmp_path / 'manifest.json').write_text(
        json.dumps(
            {
                'ships': [
                    {
                        'id': 1,
                        'name': '约克',
                        'ship_type': 'ca',
                        'portrait': 'assets/missing.png',
                    }
                ]
            }
        ),
        encoding='utf-8',
    )

    with pytest.raises(ValueError, match='头像不存在'):
        NamedPortraitMatcher(tmp_path)


def test_named_matcher_rejects_unusable_portrait_at_match_time(tmp_path: Path) -> None:
    assets = tmp_path / 'assets'
    assets.mkdir()
    portrait_path = assets / 'blank.png'
    cv2.imwrite(str(portrait_path), np.zeros((20, 20, 3), dtype=np.uint8))
    (tmp_path / 'manifest.json').write_text(
        json.dumps(
            {
                'ships': [
                    {
                        'id': 1,
                        'name': '约克',
                        'ship_type': 'ca',
                        'portrait': 'assets/blank.png',
                    }
                ]
            }
        ),
        encoding='utf-8',
    )
    matcher = NamedPortraitMatcher(tmp_path)

    with pytest.raises(ValueError, match='缺少可用特征'):
        matcher.identify(_portrait(1), '约克')
