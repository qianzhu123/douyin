from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_ROOT / "external" / "douyin-monitor"
DOWNLOADER_DIR = PROJECT_ROOT / "external" / "douyin-downloader"
SEARCH_DIR = PROJECT_ROOT / "external" / "douyin-user-search"
SEARCH_PROFILE_DIR = SEARCH_DIR / "douyin_profile"
SEARCH_HEADLESS = True
DOWNLOAD_OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_DIR = PROJECT_ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"
PROFILE_CACHE_FILE = DATA_DIR / "profile_cache.json"
