# Douyin Monitor

抖音用户信息监控工具 — 查询主页信息 & 直播状态轮询通知

## Quick Start

```bash
pip install playwright winotify
playwright install chromium
```

## Scripts

| Script | Purpose |
|--------|---------|
| `query.bat` | Query profile info (press Enter for settings.txt) |
| `watch.bat` | Watch live status with notifications (2-step input) |

## CLI

```bash
# Query
python main.py query                    # all from settings.txt
python main.py query 梦鱼               # by keyword
python main.py query "https://..."      # by URL

# Watch
python main.py watch                    # all, 30s interval
python main.py watch -i 10              # 10s interval
python main.py watch 梦鱼 -i 5          # one user, 5s interval
```

## Input Methods

| Input | Match | Example |
|-------|-------|---------|
| keyword | fuzzy match settings.txt labels | 梦鱼, Whys |
| URL | direct parse | https://www.douyin.com/user/MS4w... |
| sec_uid | direct use | MS4wLjABAAAA... |
| (empty) | all from settings.txt | - |

## Project Structure

```
douyin-monitor/
├── main.py         Core program
├── settings.txt    User config (URLs + labels)
├── query.bat       Query launcher
├── watch.bat       Watch launcher
├── data/           History (auto-created)
└── README.md
```
