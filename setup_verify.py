#!/usr/bin/env python3
"""
Quick setup verification script.
Checks that all dependencies are installed and database is initialized.
"""

import sys
import os
from pathlib import Path

def check_imports():
    """Verify all required packages are installed."""
    print("Checking imports...")
    
    required = ['pandas', 'numpy', 'requests', 'ta']
    missing = []
    
    for pkg in required:
        try:
            __import__(pkg)
            print(f"  ✓ {pkg}")
        except ImportError:
            print(f"  ✗ {pkg} (MISSING)")
            missing.append(pkg)
    
    return len(missing) == 0

def check_env():
    """Verify environment variables are set."""
    print("\nChecking environment variables...")
    
    account_id = os.getenv('OANDA_ACCOUNT_ID')
    api_token = os.getenv('OANDA_API_TOKEN')
    
    if account_id:
        print(f"  ✓ OANDA_ACCOUNT_ID = {account_id[:10]}...")
    else:
        print("  ✗ OANDA_ACCOUNT_ID not set")
    
    if api_token:
        print(f"  ✓ OANDA_API_TOKEN = {api_token[:10]}...")
    else:
        print("  ✗ OANDA_API_TOKEN not set")
    
    return bool(account_id and api_token)

def check_database():
    """Initialize database if needed."""
    print("\nInitializing database...")
    
    try:
        from pipeline_utils import init_db
        init_db()
        print("  ✓ Database ready (pipeline.db)")
        return True
    except Exception as e:
        print(f"  ✗ Database error: {e}")
        return False

def check_files():
    """Verify project files exist."""
    print("\nChecking project files...")
    
    files = [
        'pipeline_utils.py',
        'data_fetcher.py',
        'validator.py',
        'live_test.py',
        'auto_research.py',
        'risk.py',
        '.opencode/agents/researcher.md',
        'tests/__init__.py',
        'tests/test_db.py',
        'tests/test_risk.py',
    ]
    
    all_exist = True
    for f in files:
        path = Path(f)
        if path.exists():
            print(f"  ✓ {f}")
        else:
            print(f"  ✗ {f} (MISSING)")
            all_exist = False
    
    return all_exist

def main():
    print("=" * 70)
    print("Trading Strategy Pipeline - Setup Verification")
    print("=" * 70)
    
    checks = [
        ("Imports", check_imports),
        ("Environment", check_env),
        ("Files", check_files),
        ("Database", check_database),
    ]
    
    results = []
    for name, check_fn in checks:
        try:
            result = check_fn()
            results.append((name, result))
        except Exception as e:
            print(f"  ✗ Error during {name} check: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 70)
    print("Summary:")
    print("=" * 70)
    
    all_pass = all(r for _, r in results)
    
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  {name:20} {status}")
    
    if all_pass:
        print("\n✓ All checks passed! Ready to use.")
        print("\nNext steps:")
        print("  1. Use Researcher agent to generate strategy JSON")
        print("  2. Run: python validator.py <strategy.json>")
        print("  3. If passed, run: python live_test.py <strategy_id>")
        return 0
    else:
        print("\n✗ Some checks failed. Please fix issues above.")
        if not os.getenv('OANDA_ACCOUNT_ID'):
            print("\n  To fix environment:")
            print("  export OANDA_ACCOUNT_ID='your_id'")
            print("  export OANDA_API_TOKEN='your_token'")
        if not check_imports():
            print("\n  To fix imports:")
            print("  pip install -r requirements.txt")
        return 1

if __name__ == '__main__':
    sys.exit(main())
