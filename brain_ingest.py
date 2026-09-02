# -*- coding: utf-8 -*-
"""可选：把生成的纪要写入第二大脑（个人记忆库）。

默认关闭（config.toml [app] ingest_brain_memory = false）——朋友试用必须保持关闭。
打开后走第二大脑 CLI 的 add 命令；失败只记日志，绝不阻断出稿。
"""
import logging
import os
import subprocess
import sys

log = logging.getLogger("会议记录")


def ingest_minutes(cfg, markdown, title="会议纪要"):
    """ingest 开关打开时把纪要写入第二大脑。返回是否成功（失败不抛异常）。"""
    if not cfg["app"].get("ingest_brain_memory"):
        return False
    brain_dir = cfg["app"].get("brain_memory_dir") or ""
    if not brain_dir or not os.path.isdir(brain_dir):
        log.warning("[第二大脑] 目录不存在，跳过入库：%s", brain_dir)
        return False
    content = f"{title}\n\n{markdown}"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "brain_memory.cli", "add",
             "--tags", "会议纪要", "--source", "会议记录工具", content],
            cwd=brain_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        if proc.returncode != 0:
            log.warning("[第二大脑] 入库失败：%s", (proc.stderr or "")[:200])
            return False
        log.info("[第二大脑] 纪要已入库")
        return True
    except Exception as e:
        log.warning("[第二大脑] 入库异常：%s", e)
        return False
