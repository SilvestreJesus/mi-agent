import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


BASE_DIR = Path(__file__).resolve().parent

CATALOGS_FILE = BASE_DIR / "data" / "catalogs.json"
DATA_RECORDS_FILE = BASE_DIR / "data" / "data_records.json"
STATE_FILE = BASE_DIR / ".state.json"


JUB_URL = os.environ.get("JUB_API_URL", "http://localhost:5000").rstrip("/")
JUB_USER = os.environ.get("JUB_USERNAME", "invitado")
JUB_PASS = os.environ.get("JUB_PASSWORD", "invitado")