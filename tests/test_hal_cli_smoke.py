import subprocess


def test_self_fix_help_shown():
    """Run `scripts/hal.py --help` and ensure the `--self-fix` flag is present."""
    p = subprocess.run(['python3', 'scripts/hal.py', '--help'], capture_output=True, text=True)
    assert p.returncode == 0
    assert '--self-fix' in p.stdout
