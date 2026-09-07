"""
Tests for meta_review.write_steer_families + the diversity_directive structured
emission: the diversity steer is now written as JSON (boost/damp/n/ts) in addition
to the prose bullet, so a downstream scheduler can consume families, not prose.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import meta_review as mr
import pipeline_utils as pu


def _parse_iso(ts):
    return datetime.fromisoformat(ts)


class TestWriteSteerFamilies:
    def test_writes_expected_keys_and_values(self, tmp_path, monkeypatch):
        p = tmp_path / 'steer_families.json'
        monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', p)
        assert mr.write_steer_families(['cross-market', 'event'], 'volatility', 63415) is True
        data = json.loads(p.read_text())
        assert set(data.keys()) == {'boost', 'damp', 'n', 'ts'}
        assert data['boost'] == ['cross-market', 'event']
        assert data['damp'] == 'volatility'
        assert data['n'] == 63415
        _parse_iso(data['ts'])  # parseable ISO timestamp

    def test_writes_twice_replaces_not_appends(self, tmp_path, monkeypatch):
        p = tmp_path / 'steer_families.json'
        monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', p)
        mr.write_steer_families(['calendar'], 'trend', 1)
        mr.write_steer_families(['flow', 'event'], 'mean-reversion', 2)
        data = json.loads(p.read_text())
        assert data['boost'] == ['flow', 'event']
        assert data['damp'] == 'mean-reversion'
        assert data['n'] == 2

    def test_unwritable_path_returns_false_and_never_raises(self, monkeypatch):
        monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', Path('/proc/x/y.json'))
        assert mr.write_steer_families(['a'], 'b', 1) is False

    def test_os_replace_failure_returns_false_and_never_raises(self, monkeypatch, tmp_path):
        p = tmp_path / 'steer_families.json'
        monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', p)

        def boom(src, dst):
            raise OSError('nope')
        monkeypatch.setattr(mr.os, 'replace', boom)
        assert mr.write_steer_families(['a'], 'b', 1) is False


class _DiversityDB:
    """Seed a temp DB with >=50 clean-era rationales whose mechanism mix is fully
    controlled, so diversity_directive()'s structured output is deterministic."""

    def __init__(self, monkeypatch, tmp_path):
        db = tmp_path / 'pipe.db'
        monkeypatch.setattr(pu, 'DB_PATH', db)
        monkeypatch.setattr(mr, 'DB_PATH', db)
        pu.init_db()
        with pu.get_db_connection() as conn:
            c = conn.cursor()
            for i in range(120):
                c.execute(
                    "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, "
                    "timeframe, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f'vol_{i}_x', f'fp_{i}', 'code', '{}',
                     'volatility regime squeeze', 'D', 'research_failed',
                     '2026-07-01T00:00:00'),
                )
            # a small number (<5%) of cross-market rationales -> under-used bucket
            for i in range(3):
                c.execute(
                    "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, "
                    "timeframe, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f'cm_{i}_x', f'cmfp_{i}', 'code', '{}',
                     'cross-market divergence between related instruments', 'D',
                     'research_failed', '2026-07-01T00:00:00'),
                )


class TestDiversityDirectiveEmitsJSON:
    def test_boost_matches_bullet_brackets_and_damp_matches_dominant(
            self, monkeypatch, tmp_path):
        _DiversityDB(monkeypatch, tmp_path)
        out = tmp_path / 'steer_families.json'
        monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', out)

        bullet = mr.diversity_directive()
        assert bullet is not None

        # parse the brackets from the bullet text
        marker = 'under-used ['
        bracketed = bullet.split(marker, 1)[1].split(']', 1)[0]
        bullet_boost = [f.strip() for f in bracketed.split(',')]

        # dominant family is the token after "): " and before " NN% dominant"
        head = bullet.split(' dominant;', 1)[0]
        dominant = head.split('): ', 1)[1].rsplit(' ', 1)[0]

        data = json.loads(out.read_text())
        assert data['boost'] == bullet_boost
        assert data['damp'] == dominant

    def test_less_than_50_rows_returns_none_and_leaves_file_untouched(
            self, monkeypatch, tmp_path):
        # <50 rows: create a small clean-era DB lying elsewhere
        db = tmp_path / 'small.db'
        monkeypatch.setattr(pu, 'DB_PATH', db)
        monkeypatch.setattr(mr, 'DB_PATH', db)
        pu.init_db()
        with pu.get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO strategies (id, fingerprint, code, param_grid, rationale, "
                "timeframe, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ('a_1_x', 'fp_1', 'code', '{}', 'volatility squeeze', 'D',
                 'research_failed', '2026-07-01T00:00:00'),
            )

        out = tmp_path / 'steer_families.json'
        out.write_text('PRESERVED')
        monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', out)

        assert mr.diversity_directive() is None
        assert out.read_text() == 'PRESERVED'