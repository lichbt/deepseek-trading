"""Tests for categories/<name>.md — the single-source per-category instruction files.

Each generation category (macro, calendar, event, pair, asset, standard, wild) has
one md with a ## CONSTRAINT block (the injected text) + a ## GUIDANCE block (spliced
into the thesis prompt). The loader must be byte-identical to the inline fallback so
the refactor changed NOTHING the model sees, and must fail soft when a file is absent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar

CATEGORIES = ['macro', 'calendar', 'event', 'pair', 'asset', 'standard', 'wild']


class TestCategoryFiles:
    def test_all_category_files_present_and_parsed(self):
        for name in CATEGORIES:
            cat = ar._load_category(name)
            assert cat is not None, f"categories/{name}.md missing or unparseable"
            assert cat['constraint'], f"{name}.md has empty ## CONSTRAINT"
            assert cat['guidance'], f"{name}.md has empty ## GUIDANCE"

    def test_static_constraints_byte_identical_to_fallback(self):
        # Static categories: the md CONSTRAINT must equal the inline fallback exactly.
        for name in ('calendar', 'event', 'wild', 'pair'):
            assert ar._category_constraint(name) == ar._FALLBACK_CONSTRAINTS[name], name

    def test_standard_list_matches_fallback(self):
        assert ar._category_list('standard') == ar._FALLBACK_STANDARD

    def test_creative_rotation_assembled_correctly(self):
        # Public rotation = standard items + the pair constraint, pair last.
        assert ar._CREATIVE_CONSTRAINTS == ar._FALLBACK_STANDARD + [ar._FALLBACK_PAIR]
        assert 'Cross-market PAIR' in ar._CREATIVE_CONSTRAINTS[-1]
        assert len(ar._CREATIVE_CONSTRAINTS) == 10

    def test_macro_dynamic_tokens_fill(self):
        out = ar._macro_constraint_for('EUR_USD')
        assert '{instrument}' not in out and '{cols}' not in out
        assert 'EUR_USD' in out and 'macro-archetype' in out

    def test_asset_dynamic_tokens_fill_and_keep_literal_braces(self):
        out = ar._asset_mode_for('XAU_USD', seed=1)
        assert out is not None
        assert '{instrument}' not in out and '{chosen}' not in out
        # literal brace list is NOT a token — must survive verbatim
        assert '{open, high, low, close, date}' in out

    def test_fallback_when_file_missing(self, monkeypatch, tmp_path):
        # Point the loader at an empty dir and clear the cache: it must fall back
        # to the inline constants, NOT crash or return ''.
        monkeypatch.setattr(ar, '_CATEGORY_DIR', tmp_path)
        monkeypatch.setattr(ar, '_CATEGORY_CACHE', {})
        assert ar._category_constraint('calendar') == ar._FALLBACK_CONSTRAINTS['calendar']
        assert ar._category_constraint('event') == ar._FALLBACK_CONSTRAINTS['event']
        assert ar._category_list('standard') == ar._FALLBACK_STANDARD
        assert '{instrument}' not in ar._macro_constraint_for('EUR_USD')  # tokens still fill

    def test_thesis_prompt_splices_all_four_sections(self):
        rules = ar._get_thesis_rules()
        assert '<!-- CATEGORY_GUIDANCE' not in rules      # sentinel replaced
        for h in ('## Macro data', '## Cross-market', '## Economic-event data',
                  '## Calendar / seasonal', '## Microstructure data'):
            assert rules.count(h) == 1, f"{h} appears {rules.count(h)}x"
