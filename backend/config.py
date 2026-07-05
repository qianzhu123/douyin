from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONITOR_DIR = Path(os.getenv("DOUYIN_MONITOR_DIR", str(PROJECT_ROOT / "external" / "douyin-monitor")))
DOWNLOADER_DIR = Path(os.getenv("DOUYIN_DOWNLOADER_DIR", str(PROJECT_ROOT / "external" / "douyin-downloader")))
SEARCH_DIR = Path(os.getenv("DOUYIN_SEARCH_DIR", str(PROJECT_ROOT / "external" / "douyin-user-search")))
SEARCH_PROFILE_DIR = Path(os.getenv("DOUYIN_SEARCH_PROFILE_DIR", str(SEARCH_DIR / "douyin_profile")))
SEARCH_HEADLESS = os.getenv("DOUYIN_SEARCH_HEADLESS", "1").strip().lower() in {"1", "true", "yes"}
DOWNLOAD_OUTPUT_DIR = Path(os.getenv("DOUYIN_DOWNLOAD_OUTPUT", str(PROJECT_ROOT / "output")))
DATA_DIR = PROJECT_ROOT / "data"
USERS_FILE = Path(os.getenv("DOUYIN_USERS_FILE", str(DATA_DIR / "users.json")))
PROFILE_CACHE_FILE = Path(os.getenv("DOUYIN_PROFILE_CACHE_FILE", str(DATA_DIR / "profile_cache.json")))
