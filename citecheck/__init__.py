"""Citation extraction and verification toolkit."""

from pathlib import Path

__version__ = "1.0.0"


def _load_dotenv() -> None:
    """Read the project's .env so API keys work without shell setup.

    Values already present in the real environment always win, so an exported
    key is never silently overridden by a stale file.
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        import os

        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
        return
    load_dotenv(env_file, override=False)


_load_dotenv()
