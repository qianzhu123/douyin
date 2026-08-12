"""把 downloader.download_douyin 跑在子进程里，父侧加硬超时。

为什么需要子进程：downloader 内部用 sync_playwright 连常驻 Chrome(CDP)。当 CDP
Chrome 死亡时，page.* 调用会无限挂起且**不抛异常**——同进程线程里既无法
interrupt、try/except 也接不到。把 download_douyin 放进独立子进程，父侧用
subprocess.run(timeout=...) 兜底：超时直接 kill 子进程，挂死的 sync_playwright
随之被 OS 回收，backend 线程立刻得到 TimeoutExpired，job 不再永挂 running。

子进程开销约 1~2s python 启动，可接受（下载本身秒级以上）。

参数经环境变量 DOUYIN_DL_JOB（一个 JSON 对象）传给子进程，避免在命令行里拼接
含中文/特殊字符的 URL 时被 % 格式化或 shell 转义误伤。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .cdp_probe import cdp_alive
from .config import DOWNLOADER_DIR, DOWNLOAD_TIMEOUT

# 子进程引导脚本。参数从环境变量 DOUYIN_DL_JOB(JSON) 读取。
# 职责：导入 downloader、调用 download_douyin、把结果以单行 JSON 打到 stdout；
# 任何异常则打 {"__error__": ...} 并非零退出。父侧据此解析。
_BOOTSTRAP = r"""
import json, os, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
job = json.loads(os.environ["DOUYIN_DL_JOB"])
try:
    sys.path.insert(0, job["downloader_dir"])
    import downloader as d
    result = d.download_douyin(
        job["url"],
        job["output_dir"],
        int(job["mode"]),
        bool(job["comments"]),
        selected_indices=job["selected_indices"],  # list[int] or None
        wrap_folder=bool(job["wrap_folder"]),
    )
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    sys.exit(0)
except SystemExit:
    raise
except BaseException as e:
    print(json.dumps({"__error__": str(e)}, ensure_ascii=False), flush=True)
    sys.exit(1)
"""


def run_download_subprocess(
    url: str,
    output_dir: str,
    mode: int,
    comments: bool,
    selected_indices: list[int] | None,
    wrap_folder: bool,
    timeout: int | None = None,
) -> dict[str, Any]:
    """在子进程中跑 download_douyin，返回结果 dict。

    - 超时: 抛 subprocess.TimeoutExpired（_run_job 负责转成 error 日志）。
    - 下载失败: 抛 RuntimeError(原因)，含 downloader 的错误信息。
    - 成功: 返回 download_douyin 的结果 dict（已含 file_path/folder 等）。
    """
    if timeout is None:
        timeout = DOWNLOAD_TIMEOUT

    # 起子进程前探活常驻 Chrome。不可达就置 DOUYIN_CDP=""，强制 downloader 走
    # profile/裸启动回退，避免在死亡 CDP 上 connect_over_cdp 半连接挂死。
    env = dict(os.environ)
    cdp_ok = cdp_alive()
    if not cdp_ok:
        env["DOUYIN_CDP"] = ""

    job_env = {
        "url": url,
        "output_dir": output_dir,
        "mode": int(mode),
        "comments": bool(comments),
        "selected_indices": list(selected_indices) if selected_indices else None,
        "wrap_folder": bool(wrap_folder),
        "downloader_dir": str(DOWNLOADER_DIR),
    }
    env["DOUYIN_DL_JOB"] = json.dumps(job_env, ensure_ascii=False)

    cmd = [sys.executable, "-c", _BOOTSTRAP]
    # timeout 抛 TimeoutExpired(它是 SubprocessError 子类，不是 TimeoutError 子类，
    # 所以这里统一转成标准 TimeoutError，方便 _run_job 单点捕获)。subprocess.run
    # 超时会先 kill 子进程；挂死的 sync_playwright 进程随之被 OS 回收。
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=str(DOWNLOADER_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"downloader 子进程超时({timeout}s)已终止: {str(exc)[:120]}") from exc

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        msg = ""
        if out:
            try:
                parsed = json.loads(out.splitlines()[-1])
                msg = parsed.get("__error__", "")
            except Exception:
                pass
        if not msg:
            msg = err[:300] if err else f"downloader exited {proc.returncode} with no output"
        raise RuntimeError(msg)

    if not out:
        raise RuntimeError("downloader 没有输出结果")
    try:
        result = json.loads(out.splitlines()[-1])
    except Exception as e:
        raise RuntimeError(f"无法解析 downloader 输出: {e}; stdout tail={out[-200:]}")

    if isinstance(result, dict) and result.get("__error__"):
        raise RuntimeError(result["__error__"])
    if not isinstance(result, dict):
        raise RuntimeError(f"downloader 返回非 dict: {type(result).__name__}")
    return result
