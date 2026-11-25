import os
import sys
import tempfile
from pathlib import Path

# Use a throwaway database for tests and make `import db` work.
os.environ["NEXOV_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["PHONE_KEY_FILE"] = str(Path(tempfile.mkdtemp()) / "phone_key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
