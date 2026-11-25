"""backend/name_search_v2.py — 按名/抖音号搜索用户 (v2 general/single 协议通道).

2026-11-26 打通 (lab t10, commit 89c867eaf 后续):
- 端点: POST/GET https://aweme.snssdk.com/aweme/v2/search/general/single/ (App 40.6.0 真机同款)
- 2483 登录墙的根因 = 搜索域校验 bd-ticket-guard 头族 (路径绑定+时效);
  e125 真机抓包拿到新鲜完整头组 (logs/e125_parsed_headers.json, 含凭证不入库)
- 重放配方 = e125 新鲜头组 + unidbg 七头 (sessionid 与头组同会话) → sc=0 真卡
- 实测: query=吕德华 第一卡即本尊 (nickname/uid/unique_id/sec_uid 全齐)

头组时效: bd-ticket-guard-client-data 含 timestamp (设备现签); 头组失效后
2483 会回来 — 届时需再抓一次 (e125_fresh_headers.py, 真机窗口)。

对外: search_user(keyword) -> [user dict]  (nickname/uid/sec_uid/unique_id/follower_count)
纪律: 只读搜索。
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

LAB_ROOT = Path(r"D:\code\android\androidre\douyin")
FRESH_HEADERS_FILE = LAB_ROOT / "logs" / "e125_parsed_headers.json"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("com.ss.android.ugc.aweme/400601 (Linux; U; Android 11; zh_CN; "
      "Pixel 2 XL; Build/RP1A.201005.004.A1)")

_signer = None


def _signer_mod():
    global _signer
    if _signer is None:
        import importlib.util
        import sys
        sys.path.insert(0, str(LAB_ROOT / "tools"))
        spec = importlib.util.spec_from_file_location(
            "e121_unidbg_client", LAB_ROOT / "tools" / "e121_unidbg_client.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _signer = mod
    return _signer


def headers_fresh() -> bool:
    """新鲜头组文件存在且含 ticket 家族 → 可用."""
    try:
        h = json.load(open(FRESH_HEADERS_FILE, encoding="utf-8"))
        return "bd-ticket-guard-client-data" in h and "X-Tt-Token" in h
    except Exception:
        return False


def _common_q(**extra) -> dict:
    q = dict(iid="1765185923070852", device_id="5967213509056", ac="wifi",
             channel="huawei_1128_64", aid=1128, app_name="aweme",
             version_code=400600, version_name="40.6.0", device_platform="android",
             os="android", ssmix="a", device_type="Pixel 2 XL", device_brand="google",
             language="zh", os_api=30, os_version=11, manifest_version_code=400601,
             resolution="1440*2712", dpi=560, update_version_code=40609900,
             _rticket=int(time.time() * 1000), package="com.ss.android.ugc.aweme",
             first_launch_timestamp=0, last_deeplink_update_version_code=40609900,
             cpu_support64="true", host_abi="arm64-v8a", is_guest_mode=0,
             app_type="normal", minor_status=0, appTheme="light", is_preinstall=0,
             need_personal_recommend=1, is_android_pad=0, is_android_fold=0,
             ts=int(time.time()), cdid="61eafaf6-6a6b-4423-bf00-bfa1b53b1fb5")
    q.update(extra)
    return q


def _extract_users(d: dict) -> list[dict]:
    """深扫 v2 响应收 (nickname+sec_uid|uid) 齐全的作者对象, 保序去重."""
    out: list[dict] = []
    seen: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            nick = o.get("nickname")
            sec = o.get("sec_uid")
            uid = o.get("uid") or o.get("id_str") or o.get("id")
            if nick and (sec or uid):
                key = str(sec or uid)
                if key not in seen:
                    seen.add(key)
                    out.append({
                        "nickname": nick,
                        "uid": str(uid) if uid else None,
                        "sec_uid": sec,
                        "unique_id": o.get("unique_id") or o.get("short_id"),
                        "follower_count": o.get("follower_count"),
                    })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(d)
    return out


def search_user(keyword: str, count: int = 10) -> list[dict]:
    """按名/抖音号搜索用户卡. 失败/墙/头组缺失 → [] (调用方回落 sug/浏览器链)."""
    if not headers_fresh():
        return []
    fresh = json.load(open(FRESH_HEADERS_FILE, encoding="utf-8"))
    ck = fresh.get("Cookie", "")
    m_sid = re.search(r"sessionid=([^;]+)", ck)
    m_iid = re.search(r"install_id=([^;]+)", ck)
    if not (m_sid and m_iid):
        return []

    params = _common_q(
        query=keyword, count=count, offset=0,
        search_id=f"{int(time.time() * 1000)}7000000000000000000",
        search_channel="aweme_general", enter_from="search_page",
        is_global_rewrite=0, history_vid="", match_user_keyword=1,
        sort_type=0, publish_time=0, filter_type=0)
    url = ("https://aweme.snssdk.com/aweme/v2/search/general/single/?"
           + urllib.parse.urlencode(params))

    try:
        mod = _signer_mod()
        minjar = f"install_id={m_iid.group(1)}\r\nsessionid={m_sid.group(1)}"
        heads = mod.unidbg_sign(url, minjar)
    except Exception:
        return []

    headers = dict(fresh)
    headers["User-Agent"] = UA
    headers.update(heads)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(d, dict) or d.get("status_code") != 0:
        return []
    return _extract_users(d)
