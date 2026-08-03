"""Hand-editable generation steering, loaded from steering.md.

WHY THIS EXISTS (2026-08-03). meta_review writes a prose directive into
thesis.md and the thesis model reads it, but prose CANNOT move instrument or
timeframe selection: both are decided in Python, in _build_batch_schedule,
before the model is ever called. Measured over 922 generations on 2026-08-03,
the live directive said "Focus on XAU_USD and NZD_USD" and XAU_USD came out the
SECOND-LOWEST of 31 instruments (26 gens against a 30 mean); it also said "avoid
H1/H4" while _TIMEFRAME_ROTATION forced them onto 40% of every batch. A feedback
loop whose instruction the scheduler structurally ignores is not a feedback
loop. Knobs the SCHEDULER obeys therefore live here; advice for the MODEL stays
in thesis.md.

FAIL-SOFT IS THE POINT. This runs unattended overnight, so every failure path
returns defaults and warns rather than raising: a missing file, an unreadable
file, a malformed YAML block, a bad type, an unknown timeframe. Generation
running on defaults is always better than generation not running.

NOT CACHED, deliberately: load() re-reads on every call so an edit takes effect
on the NEXT batch without restarting the loop. It is called once per batch, so
the read costs nothing.
"""
from pathlib import Path
from typing import List, Optional

STEERING_PATH = Path(__file__).parent / 'steering.md'

# Kept in sync with auto_research._VALID_TIMEFRAMES. Duplicated rather than
# imported to keep this module free of an auto_research import (auto_research
# imports THIS, and a cycle would be resolvable but pointless).
_VALID_TIMEFRAMES = {'M30', 'H1', 'H4', 'D', 'W'}

# Built-in fallbacks. These are the values used when steering.md is absent,
# empty or malformed, so they must stand alone as a sane configuration.
DEFAULT_TIMEFRAME_ROTATION = ['D', 'H4', 'D', 'D', 'D', 'H4', 'D', 'D', 'D', 'W']
DEFAULT_FOCUS_SLOT_EVERY = 10


class Steering:
    """Resolved, validated steering knobs. Always constructible."""

    __slots__ = ('focus_instruments', 'avoid_instruments',
                 'focus_slot_every', 'timeframe_rotation')

    def __init__(self, focus_instruments=None, avoid_instruments=None,
                 focus_slot_every=DEFAULT_FOCUS_SLOT_EVERY,
                 timeframe_rotation=None):
        self.focus_instruments = list(focus_instruments or [])
        self.avoid_instruments = list(avoid_instruments or [])
        self.focus_slot_every = focus_slot_every
        self.timeframe_rotation = list(timeframe_rotation
                                       or DEFAULT_TIMEFRAME_ROTATION)

    def __repr__(self):
        return (f'Steering(focus={self.focus_instruments}, '
                f'avoid={self.avoid_instruments}, '
                f'every={self.focus_slot_every}, '
                f'tf={self.timeframe_rotation})')


def _warn(msg: str) -> None:
    print(f'  [steering] {msg}', flush=True)


def _extract_yaml_block(text: str) -> Optional[str]:
    """Return the first ```yaml fenced block, or None.

    The fence must start a LINE. A naive text.find('```yaml') matches the same
    marker written inline in prose — which is not hypothetical: the first draft
    of steering.md described its own format in a sentence, so the extractor
    started mid-prose and every edit to the file was silently ignored (the load
    warned and fell back to defaults, so the file LOOKED fine and did nothing).

    Only the FIRST block is read, so examples further down can never become
    config.
    """
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if start is None:
            if line.strip().lower() in ('```yaml', '```yml'):
                start = idx + 1
        elif line.strip().startswith('```'):
            return '\n'.join(lines[start:idx])
    return None   # no opening fence, or an unterminated one


def _clean_symbols(raw, field: str) -> List[str]:
    """Coerce a YAML list into a list of upper-case instrument symbols."""
    if raw is None:
        return []
    if isinstance(raw, str):          # tolerate `focus_instruments: XAU_USD`
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        _warn(f'{field} must be a list — ignoring it')
        return []
    out = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            _warn(f'{field}: dropping non-symbol entry {item!r}')
            continue
        out.append(item.strip().upper())
    return out


def load(path: Path = None) -> Steering:
    """Read steering.md and return validated knobs. Never raises."""
    path = path or STEERING_PATH
    try:
        if not path.exists():
            return Steering()
        block = _extract_yaml_block(path.read_text())
        if block is None:
            _warn(f'{path.name} has no ```yaml block — using defaults')
            return Steering()
        import yaml
        data = yaml.safe_load(block)
        if data is None:
            return Steering()
        if not isinstance(data, dict):
            _warn(f'{path.name} yaml block is not a mapping — using defaults')
            return Steering()
    except Exception as e:
        # Covers a missing pyyaml, unreadable file and malformed yaml alike.
        _warn(f'could not read {getattr(path, "name", path)} ({e}) — using defaults')
        return Steering()

    focus = _clean_symbols(data.get('focus_instruments'), 'focus_instruments')
    avoid = _clean_symbols(data.get('avoid_instruments'), 'avoid_instruments')

    # AVOID WINS over focus. An instrument in both is a contradiction, and
    # honouring focus would over-sample something the user asked to remove —
    # the risk-increasing reading of an ambiguous config, which is the one to
    # refuse.
    both = [i for i in focus if i in avoid]
    if both:
        _warn(f'{both} listed in BOTH focus and avoid — avoid wins')
        focus = [i for i in focus if i not in avoid]

    every = data.get('focus_slot_every', DEFAULT_FOCUS_SLOT_EVERY)
    # bool is an int subclass; exclude it so `focus_slot_every: true` is caught.
    if isinstance(every, bool) or not isinstance(every, int) or every < 1:
        _warn(f'focus_slot_every must be an integer >= 1 (got {every!r}) — '
              f'using {DEFAULT_FOCUS_SLOT_EVERY}')
        every = DEFAULT_FOCUS_SLOT_EVERY

    rotation = data.get('timeframe_rotation')
    if rotation is None:
        rotation = list(DEFAULT_TIMEFRAME_ROTATION)
    else:
        if isinstance(rotation, str):
            rotation = [rotation]
        if not isinstance(rotation, (list, tuple)):
            _warn('timeframe_rotation must be a list — using the default')
            rotation = list(DEFAULT_TIMEFRAME_ROTATION)
        else:
            cleaned = []
            for tf in rotation:
                tf_u = str(tf).strip().upper()
                if tf_u in _VALID_TIMEFRAMES:
                    cleaned.append(tf_u)
                else:
                    _warn(f'dropping unknown timeframe {tf!r} '
                          f'(valid: {sorted(_VALID_TIMEFRAMES)})')
            if not cleaned:
                _warn('timeframe_rotation has no valid entries — using the default')
                cleaned = list(DEFAULT_TIMEFRAME_ROTATION)
            rotation = cleaned

    return Steering(focus_instruments=focus, avoid_instruments=avoid,
                    focus_slot_every=every, timeframe_rotation=rotation)
