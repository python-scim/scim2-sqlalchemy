import subprocess
import sys


def test_import_without_greenlet():
    """The package imports without the asyncio extra, which brings greenlet."""
    code = (
        "import sys\n"
        "sys.modules['greenlet'] = None\n"
        "from scim2_sqlalchemy import SqlAlchemyStorage\n"
        "from scim2_sqlalchemy import AsyncSqlAlchemyStorage\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
