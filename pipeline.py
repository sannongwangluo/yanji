# -*- coding: utf-8 -*-
"""管线编排。

主路径（GUI 默认）：边录边出的流式识别——散会时把流式收集好的 utterances 直接
走「报到映射 → DeepSeek 纪要 → docx」（run_pipeline_streaming）。

备用路径（--file-mode）：音频文件 → 上传 TOS → 文件识别 → 映射 → 纪要 → docx
（run_pipeline，保留 asr_client.py 不动）。

GUI 在后台线程里调 run_pipeline_streaming()，经 progress 回调（写队列）更新界面；
命令行也可用（验收测试入口）：
    python pipeline.py --test-stream-wav <wav文件>   # 流式真实链路验收（200ms 实时喂）
    python pipeline.py <wav文件>                     # 文件识别路径（备用）
    python pipeline.py --mock-asr tests/fake_asr_result.json
    python pipeline.py --audio-url <公网音频URL> [--format wav]  # 跳过 TOS 上传
"""
import argparse
import json
import logging
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from asr_client import AsrClient, parse_utterances
from config_loader import ConfigError, load_config
from docx_writer import save_minutes_pair
from minutes_llm import generate_minutes
from speaker_map import _map_speakers
from streaming_asr import MeetingStreamSession

log = logging.getLogger("会议记录")

# 状态 → GUI 状态行文案（单一来源，GUI 直接引用）
STATE_TEXT = {
    "finishing": "收尾中",
    "uploading": "上传中",
    "recognizing": "识别中",
    "mapping": "生成纪要中",
    "minutes": "生成纪要中",
    "done": "完成",
    "error": "出错",
}


def _out_dir(cfg):
    """纪要输出目录：cfg["app"]["output_dir"] 非空用它，否则默认 BASE_DIR/输出。

    单一来源：run_pipeline / run_pipeline_streaming 出稿和 GUI 显示都走这里。
    """
    out = (cfg.get("app") or {}).get("output_dir") or ""
    return out or os.path.join(BASE_DIR, "输出")


def setup_logging():
    """日志写 日志/会议记录_YYYYMMDD.log + 控制台。"""
    log_dir = os.path.join(BASE_DIR, "日志")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, time.strftime("会议记录_%Y%m%d.log"))
    root = logging.getLogger()
    if root.handlers:  # 已配置过就不重复
        return path
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return path


def run_pipeline(wav_path=None, progress=None, cfg=None,
                 mock_asr_file=None, audio_url=None, audio_fmt=None):
    """备用文件识别管线（--file-mode）。progress(state, detail) 回调；返回 (docx_path, 报告 dict)。

    任何错误都抛中文消息的异常（ConfigError/RuntimeError/RecorderError 等），
    由调用方决定怎么展示；栈 trace 只进日志，不上界面。
    """
    cfg = cfg or load_config()
    t0 = time.monotonic()

    # 1. 识别（真实云端 / 本地假结果测试模式）
    if mock_asr_file:
        with open(mock_asr_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info("[识别] 测试模式：读本地假识别结果 %s", mock_asr_file)
        if progress:
            progress("recognizing", "测试模式（本地假识别结果）")
    else:
        client = AsrClient(cfg)
        data = client.transcribe(
            wav_path=wav_path, audio_url=audio_url, fmt=audio_fmt, progress=progress)

    utterances = parse_utterances(data)
    if not utterances:
        raise RuntimeError("识别结果为空：音频里可能没有有效人声，请重录一次。")
    log.info("[识别] 共 %d 句分句", len(utterances))

    # 2. 报到映射（纯函数，免声纹）
    if progress:
        progress("mapping", "")
    mapping, renamed = _map_speakers(utterances)
    log.info("[映射] 报到映射：%s", mapping or "（无人报到，全部按 说话人N 显示）")

    # 3. DeepSeek 生成纪要
    if progress:
        progress("minutes", "（DeepSeek 思考中，通常一两分钟）")
    markdown = generate_minutes(cfg, renamed)

    # 4. 成对导出（Word + Markdown 同名成对）
    out_dir = _out_dir(cfg)
    docx_path, md_path = save_minutes_pair(markdown, out_dir)

    # 5. 可选：第二大脑入库（默认关，失败不阻断）
    try:
        from brain_ingest import ingest_minutes
        ingest_minutes(cfg, markdown)
    except Exception:
        log.exception("[第二大脑] 入库失败（不阻断出稿）")

    if progress:
        progress("done", "")
    log.info("[完成] 总耗时 %.1f 秒，纪要：%s（md：%s）",
             time.monotonic() - t0, docx_path, md_path)
    return docx_path, {"mapping": mapping, "utterance_count": len(utterances),
                       "md_path": md_path}


def run_pipeline_streaming(utterances, progress=None, cfg=None):
    """流式主路径散会管线：收集好的 utterances → 报到映射 → DeepSeek → docx+md。

    识别环节在开会时已由 MeetingStreamSession 实时完成（并已逐句落盘），
    散会到这里只剩 映射→纪要→成对导出，复用 _map_speakers / generate_minutes /
    save_minutes_pair，与文件识别路径同一套逻辑（单一来源）。
    """
    cfg = cfg or load_config()
    t0 = time.monotonic()
    if not utterances:
        raise RuntimeError(
            "这次会议没有实时转写出任何内容。\n"
            "可能原因：流式识别没连上/中途断开且重连失败、或全程静音。\n"
            "可用备用文件识别路径补出稿（需要 [tos] 配置）：\n"
            "  python pipeline.py 录音\\会议录音_xxxxxx.wav")
    log.info("[转写] 流式共收集 %d 句 definite 分句", len(utterances))

    # 1. 报到映射（纯函数，免声纹；散会用全量重算，与实时增量映射同一套规则）
    if progress:
        progress("mapping", "")
    mapping, renamed = _map_speakers(utterances)
    log.info("[映射] 报到映射：%s", mapping or "（无人报到，全部按 说话人N 显示）")

    # 2. DeepSeek 生成纪要
    if progress:
        progress("minutes", "（DeepSeek 思考中，通常一两分钟）")
    markdown = generate_minutes(cfg, renamed)

    # 3. 成对导出（Word + Markdown 同名成对）
    out_dir = _out_dir(cfg)
    docx_path, md_path = save_minutes_pair(markdown, out_dir)

    # 4. 可选：第二大脑入库（默认关，失败不阻断）
    try:
        from brain_ingest import ingest_minutes
        ingest_minutes(cfg, markdown)
    except Exception:
        log.exception("[第二大脑] 入库失败（不阻断出稿）")

    if progress:
        progress("done", "")
    log.info("[完成] 总耗时 %.1f 秒，纪要：%s（md：%s）",
             time.monotonic() - t0, docx_path, md_path)
    return docx_path, {"mapping": mapping, "utterance_count": len(utterances),
                       "md_path": md_path}


def _test_stream_wav(wav_path):
    """流式真实链路验收：wav → 重采样 16k → 按 200ms 实时 pacing 喂给
    MeetingStreamSession，打印每个 definite utterance（speaker+text）和最终句数。

    返回进程退出码（0=有分句，1=失败/无分句）。
    """
    import wave

    import numpy as np

    cfg = load_config()
    if not os.path.exists(wav_path):
        print(f"失败：找不到音频文件 {wav_path}", file=sys.stderr)
        return 1
    with wave.open(wav_path, "rb") as w:
        src_rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        print("失败：只支持 16-bit WAV", file=sys.stderr)
        return 1
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0].copy()
    if src_rate != 16000:
        n_out = int(len(pcm) * 16000 / src_rate)
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n_out),
                        np.arange(len(pcm)), pcm).astype(np.float32)
        print(f"[测试] 音频 {src_rate}Hz → 16kHz 重采样（线性插值），"
              f"{len(pcm) / 16000:.1f} 秒", flush=True)

    count = [0]

    def on_utterance(u):
        count[0] += 1
        print(f"  [{count[0]:02d}] speaker={u['speaker']} {u['text']}", flush=True)

    session = MeetingStreamSession(cfg)
    session.start(on_utterance=on_utterance)
    chunk = 16000 * 200 // 1000  # 200ms
    t0 = time.monotonic()
    for i in range(0, len(pcm), chunk):
        session.feed(pcm[i:i + chunk])
        elapsed = time.monotonic() - t0
        target = (i + chunk) / 16000.0
        if target > elapsed:  # 按真实开会节奏 pacing（200ms/包）
            time.sleep(target - elapsed)
    session.finish()

    utts = session.utterances
    dist = {}
    for u in utts:
        dist[u["speaker"]] = dist.get(u["speaker"], 0) + 1
    print(f"\n[测试] 共 {len(utts)} 句 definite 分句；speaker 分布：{dist}", flush=True)
    if session.failed:
        print(f"[测试] 注意：会话中途重连失败（{session.last_error}），"
              "以上为断开前的分句", flush=True)
    if session.jsonl_path:
        print(f"[测试] 转写已落盘：{session.jsonl_path}", flush=True)
    return 0 if utts else 1


def main(argv=None):
    setup_logging()
    parser = argparse.ArgumentParser(
        description="会议记录管线。默认主路径=流式实时识别（开会时边录边出字），"
                    "本 CLI 主要用于验收和备用路径。")
    parser.add_argument("wav", nargs="?", help="录音 wav 文件（文件识别备用路径用）")
    parser.add_argument("--file-mode", action="store_true",
                        help="显式走文件识别路径（上传 TOS→提交→轮询；流式不可用时的备用）")
    parser.add_argument("--test-stream-wav", metavar="WAV",
                        help="流式真实链路验收：读 wav 按 200ms 实时喂给流式会话，"
                             "打印每个 definite 分句")
    parser.add_argument("--mock-asr", metavar="JSON", help="识别环节读本地假识别结果（测试用）")
    parser.add_argument("--audio-url", metavar="URL", help="跳过 TOS 上传，直接用公网音频 URL（调试用）")
    parser.add_argument("--format", dest="fmt", help="audio-url 模式下指定音频格式（如 wav）")
    args = parser.parse_args(argv)

    if args.test_stream_wav:
        return _test_stream_wav(args.test_stream_wav)

    try:
        path, report = run_pipeline(
            wav_path=args.wav,
            progress=lambda s, d: print(f"  [{STATE_TEXT[s]}] {d}".rstrip(), flush=True),
            mock_asr_file=args.mock_asr,
            audio_url=args.audio_url,
            audio_fmt=args.fmt,
        )
    except (ConfigError, RuntimeError, OSError) as e:
        print(f"\n失败：{e}", file=sys.stderr)
        return 1
    print(f"\n已生成 Word：{path}")
    print(f"已生成 Markdown：{report['md_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
