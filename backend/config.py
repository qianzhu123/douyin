from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_ROOT / "external" / "douyin-monitor"
DOWNLOADER_DIR = PROJECT_ROOT / "external" / "douyin-downloader"
SEARCH_DIR = PROJECT_ROOT / "external" / "douyin-user-search"
SEARCH_PROFILE_DIR = SEARCH_DIR / "douyin_profile"
SEARCH_HEADLESS = True
DOWNLOAD_OUTPUT_DIR = Path.home() / "Downloads"
DATA_DIR = PROJECT_ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"
PROFILE_CACHE_FILE = DATA_DIR / "profile_cache.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

# 下载任务硬性总超时（秒）。downloader 内部用 sync_playwright，CDP 常驻 Chrome
# 死亡时 page.* 调用会无限挂起而不抛异常；_run_job 通过子进程隔离 + 此超时兜底，
# 超时即 kill 子进程并把 job 标 error，不再永久卡在 running。
DOWNLOAD_TIMEOUT = 240

# 常驻调试 Chrome 的 CDP 端点，由 scripts/start-douyin.ps1 的 Ensure-CdpChrome 拉起，
# downloader 经 connect_over_cdp 复用它拿登录态直链。backend 用 CDP_PROBE_URL 探活。
CDP_PROBE_URL = "http://127.0.0.1:9222/json/version"
