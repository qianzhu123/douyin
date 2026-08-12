"""常驻调试 Chrome(9222) 健康探测。

下载前快速探活 CDP /json/version。关键：用 httpx 短超时，**绝不能挂起**
（之前裸 curl 无超时会卡死在死亡 WS 服务上）。不可达返回 False，由调用方决定
是走 profile 回退还是直接失败。重启常驻 Chrome 不在此处做——交给用户重新运行
scripts/start-douyin.ps1（避免与 user-search 抢同一 profile 锁）。
"""
from __future__ import annotations

import httpx

from .config import CDP_PROBE_URL


def cdp_alive(timeout: float = 2.0) -> bool:
    """CDP 常驻 Chrome 是否可达。任何异常/超时都返回 False，绝不抛、绝不挂。"""
    try:
        resp = httpx.get(CDP_PROBE_URL, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False
