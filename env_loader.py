"""Load .env into os.environ — tiny, no python-dotenv dependency.

Existing environment variables WIN (so a value sourced from ~/.zshrc is never
overridden by .env). Import for the side effect:  `import env_loader`

.env format: one KEY=value per line, # comments and blank lines ignored,
surrounding quotes stripped, and a leading `export ` tolerated. See .env
(gitignored — holds real secrets).
"""
import os
from pathlib import Path


def load_env(path=None) -> int:
    """Populate os.environ from a .env file (non-overwriting). Returns # of vars set."""
    p = Path(path) if path else Path(__file__).parent / '.env'
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        key = key.strip()
        # Tolerate `export KEY=value`. Sourcing .env from a shell makes those lines
        # work, so the file looks fine and the failure is invisible: without this,
        # the key becomes the literal 'export KEY' and os.getenv('KEY') returns
        # None everywhere .env is LOADED rather than sourced (launchd, cron, the
        # Zeabur pod). Found 2026-07-28 with 9 vars affected, incl. OANDA_API_TOKEN
        # and FIX_PASSWORD — same silent-drift class as the FRED_API_KEY gap.
        if key.startswith('export ') or key.startswith('export\t'):
            key = key[len('export'):].strip()
        val = val.strip()
        # Strip a trailing ` # comment`, but only when the # follows whitespace so
        # values that legitimately contain one (tokens, passwords) survive.
        # Without this, `FIX_START_EQUITY=2500  # account size` reaches
        # fix_adapter as the whole string and float() raises at import — it broke
        # collection of the entire test suite (2026-07-25).
        if val[:1] not in ('"', "'"):
            for sep in (' #', '\t#'):
                if sep in val:
                    val = val.split(sep, 1)[0].rstrip()
        val = val.strip('"').strip("'")
        if key and key not in os.environ:   # real env / ~/.zshrc takes precedence
            os.environ[key] = val
            n += 1
    return n


load_env()  # run on import


if __name__ == '__main__':
    # demo/self-check: a .env value loads, but never overrides an existing env var
    import tempfile
    d = tempfile.mkdtemp()
    Path(d, '.env').write_text(
        'DEMO_KEY_XYZ="hello"\n'
        'export DEMO_EXPORTED_XYZ="world"\n'
        'export DEMO_INLINE_XYZ=42  # trailing comment\n'
        'PATH="SHOULD_NOT_WIN"\n'
        '# comment\n'
    )
    for k in ('DEMO_KEY_XYZ', 'DEMO_EXPORTED_XYZ', 'DEMO_INLINE_XYZ'):
        os.environ.pop(k, None)
    real_path = os.environ.get('PATH')
    load_env(Path(d, '.env'))
    assert os.environ['DEMO_KEY_XYZ'] == 'hello', 'new key should load'
    assert os.environ['DEMO_EXPORTED_XYZ'] == 'world', '`export KEY=` must load as KEY'
    assert os.environ['DEMO_INLINE_XYZ'] == '42', 'export + inline comment must both strip'
    assert not [k for k in os.environ if k.startswith('export ')], 'no "export X" keys'
    assert os.environ['PATH'] == real_path, 'existing env must NOT be overridden'
    print('ok')
