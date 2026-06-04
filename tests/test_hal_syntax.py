import py_compile


def test_hal_compiles():
    """Ensure the main HAL CLI is syntactically valid."""
    py_compile.compile('scripts/hal.py', doraise=True)
