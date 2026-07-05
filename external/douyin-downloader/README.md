# Douyin Watermark-Free Downloader

A command-line tool to download Douyin (TikTok China) videos and image collections without watermarks, using browser automation to extract direct CDN links from the web version.

## How It Works

**Video**: The `<video>` element on the web version loads a direct CDN link that is already the original, watermark-free video.

**Image Collection**: When the web version renders an image collection, the `<img>` elements' `src` attributes point to the original CDN images. By browsing through all images (triggering lazy loading), all image URLs can be collected. Downloads use the browser context's API request (automatically carrying cookies) to bypass CDN referer validation.

## Features

- Download watermark-free videos (MP4) and image collections (webp/jpg/png)
- Automatically detect content type (video vs image collection)
- Support multiple URL inputs at once
- Support reading URLs from a file (batch mode)
- Extract Douyin URLs from share text (messy paste format)
- Specify custom output directory
- Cross-platform (Windows, macOS, Linux)

## Installation

### 1. Install Python dependencies

```bash
pip install playwright httpx
```

### 2. Install Chromium for Playwright

```bash
playwright install chromium
```

## Usage

### Single URL

```bash
python main.py https://v.douyin.com/xxxxx/
```

### Multiple URLs

```bash
python main.py https://v.douyin.com/aaaa/ https://v.douyin.com/bbbb/
```

### Read URLs from a file

Create a text file with one URL per line (lines starting with `#` are ignored):

```text
# my_urls.txt
https://v.douyin.com/aaaa/
https://v.douyin.com/bbbb/

# This line is a comment
https://www.douyin.com/video/7xxx
```

Then run:

```bash
python main.py -f my_urls.txt
```

### Specify output directory

```bash
python main.py https://v.douyin.com/xxxxx/ -o ./my_downloads
```

### Combine file and direct URLs

```bash
python main.py -f my_urls.txt https://v.douyin.com/cccc/ -o ./output
```

### Windows: double-click start.bat

1. Double-click `start.bat`
2. Choose mode (1 = paste URL, 2 = load from file)
3. Enter URL or file path
4. Press Enter to use default output directory (`./downloads/`)
5. Wait for download to complete

## Supported URL Formats

| Format | Example |
|--------|---------|
| Short link | `https://v.douyin.com/xxxxx/` |
| Full video link | `https://www.douyin.com/video/7xxx` |
| Full image collection link | `https://www.douyin.com/note/7xxx` |
| Share text (auto-extracted) | `6.99 02/11 hOk:/ ... https://v.douyin.com/xxxxx/` |

## Output Structure

```
downloads/
├── video_title.mp4                  # Video download
└── image_collection_title/           # Image collection download
    ├── 001.webp
    ├── 002.webp
    └── 003.jpg
```

## Command-Line Options

```
usage: main.py [-h] [-f FILE] [-o OUTPUT] [urls ...]

positional arguments:
  urls                  Douyin URL(s) or share text (supports multiple)

options:
  -h, --help            show this help message and exit
  -f FILE, --file FILE  Read URLs from a text file (one URL per line, # comments supported)
  -o OUTPUT, --output OUTPUT
                        Output directory (default: ./downloads/)
```

## Notes

- CDN direct links are time-limited; the program downloads immediately after extraction
- For large image collections (50+ images), browsing through all images may take some time
- For personal learning use only; please respect original creators' copyrights
- If the tool stops working due to Douyin page structure changes, selector logic may need updating

## License

MIT
