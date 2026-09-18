# -*- coding: utf-8 -*-
"""管线编排。

主路径（GUI 默认）：边录边出的流式识别——散会时把流式收集好的 utterances 直接
走「报到映射 → DeepSeek 纪要 → docx」（run_pipeline_streaming）。

备用路径（音频文件）：上传 TOS → 文件识别 → 映射 → 纪要 → docx
（run_pipeline，保留 asr_client.py 不动）——CLI 默认即文件识别路径。

GUI 在后台线程里调 run_pipeline_streaming()，经 progress 回调（写队列）更新界面；
命令行也可用（验收测试入口；CLI 不带 --test-stream-wav 就只走文件识别路径）：
    python pipeline.py --test-stream-wav <wav文件>   # 流式真实链路验收（200ms 实时喂）
    python pipeline.py <wav文件>                     # 文件识别路径
    python pipeline.py --mock-asr tests/fake_asr_result.json
    python pipeline.py --audio-url <公网音频URL> [--format wav]  # 跳过 TOS 上传
"""
import argparse
import json
import logging
import os
import re
import sys
import time

from config_loader import ConfigError, app_base_dir, load_config
from hotwords import load_hotwords

BASE_DIR = app_base_dir()
sys.path.insert(0, BASE_DIR)

from asr_client import AsrClient, parse_utterances
from docx_writer import (insert_summary_image, next_base_name,
                         save_minutes_pair, save_pdf_from_docx)
from minutes_llm import generate_minutes, meeting_base_ms
from speaker_map import _map_speakers
from streaming_asr import MeetingStreamSession

log = logging.getLogger("会议记录")

# 文件名里的开会时间戳：会议录音_20260902_1954.wav / transcript_20260916_1546.jsonl
_MEETING_STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})")


def meeting_time_from_text(text):
    """从文件名/路径解析开会时间 → "YYYY-MM-DD HH:MM"；解析不到返回 None。

    CLI 没有「点开始录音」这个时刻（GUI 有），就认文件名里的时间戳；
    解析不出（或不是合法日期）就交给 generate_minutes 写「未记录」。
    """
    if not text:
        return None
    m = _MEETING_STAMP_RE.search(os.path.basename(str(text)))
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    try:
        parsed = time.strptime(f"{y}{mo}{d}{h}{mi}", "%Y%m%d%H%M")
    except ValueError:
        return None
    return time.strftime("%Y-%m-%d %H:%M", parsed)

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


def _build_summary_image(cfg, markdown, base):
    """生成「总结信息图」PNG 并在 md 里插一行引用（best-effort，失败原样返回 md）。

    任何环节（LLM/JSON/字体/绘图）出错都只记日志：docx/md/PDF 文字版照常出稿。
    """
    try:
        from infographic import make_summary_image
    except Exception:
        log.exception("[信息图] 模块不可用（跳过信息图，不影响出稿）")
        return markdown, None
    png_path = base + "_总结图.png"
    try:
        made = make_summary_image(cfg, markdown, png_path)
    except Exception:
        log.exception("[信息图] 生成异常（跳过信息图，不影响出稿）")
        return markdown, None
    if not made:
        return markdown, None
    return insert_summary_image(markdown, os.path.basename(made)), made


def _export_pdf(docx_path):
    """docx → PDF（本机 WPS/Word COM，best-effort）。失败返回 None，绝不影响出稿。

    save_pdf_from_docx 内部已经把异常都吞了；这里再包一层是红线要求的双保险：
    PDF 任何问题都不许把已经生成的 docx/md 拖下水。
    """
    try:
        return save_pdf_from_docx(docx_path)
    except Exception:
        log.exception("[PDF] 导出异常（docx/md 已生成，不影响出稿）")
        return None


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
                 mock_asr_file=None, audio_url=None, audio_fmt=None,
                 meeting_time=None, hotwords=None):
    """备用文件识别管线（CLI 默认路径）。progress(state, detail) 回调；返回 (docx_path, 报告 dict)。

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
    markdown = generate_minutes(cfg, renamed, hotwords=hotwords,
                                meeting_time=meeting_time)

    # 4. 导出：总结信息图（best-effort）+ docx/md 同名成对 + PDF
    out_dir = _out_dir(cfg)
    base = next_base_name(out_dir)
    markdown, image_path = _build_summary_image(cfg, markdown, base)
    docx_path, md_path = save_minutes_pair(markdown, out_dir, base_name=base)
    pdf_path = _export_pdf(docx_path)

    if progress:
        progress("done", "")
    log.info("[完成] 总耗时 %.1f 秒，纪要：%s（md：%s；pdf：%s；信息图：%s）",
             time.monotonic() - t0, docx_path, md_path, pdf_path or "未生成",
             image_path or "未生成")
    return docx_path, {"mapping": mapping, "utterance_count": len(utterances),
                       "md_path": md_path, "pdf_path": pdf_path,
                       "image_path": image_path}


def run_pipeline_streaming(utterances, progress=None, cfg=None, meeting_time=None,
                           hotwords=None, digest=None, digest_covered=0):
    """流式主路径散会管线：收集好的 utterances → 报到映射 → DeepSeek → docx+md。

    识别环节在开会时已由 MeetingStreamSession 实时完成（并已逐句落盘），
    散会到这里只剩 映射→纪要→成对导出，复用 _map_speakers / generate_minutes /
    save_minutes_pair，与文件识别路径同一套逻辑（单一来源）。

    长会议（≥30 分钟）会带着中途预写的滚动摘要：digest 非空且 digest_covered>0 时，
    只把「摘要之后的新转写」发给模型（摘要当前半场），出稿更快；这条路径任何异常都
    吞掉记日志并回退到整稿路径——滚动摘要绝不能影响出稿主路径。
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

    # 2. DeepSeek 生成纪要（有滚动摘要先走「摘要 + 新转写」，失败回退整稿）
    #    base_start_ms = 整场会议第一句的时间零点：摘要路径只发后半段转写，
    #    必须显式传零点，否则「智能章节」的时间戳会从后半段重新数。
    base_ms = meeting_base_ms(renamed)
    markdown = None
    digest_text = (digest or "").strip()
    if digest_text and 0 < digest_covered < len(renamed):
        try:
            if progress:
                progress("minutes", "（续写纪要：滚动摘要 + 新转写，通常更快）")
            markdown = generate_minutes(cfg, renamed[digest_covered:], hotwords=hotwords,
                                        meeting_time=meeting_time, prior_digest=digest_text,
                                        base_start_ms=base_ms)
            log.info("[纪要] 走滚动摘要路径出稿：前 %d 句用摘要、%d 句新转写",
                     digest_covered, len(renamed) - digest_covered)
        except Exception:
            log.exception("[纪要] 滚动摘要路径出稿失败，回退整稿路径（不影响出稿）")
            markdown = None
    if markdown is None:
        if progress:
            progress("minutes", "（DeepSeek 思考中，通常一两分钟）")
        markdown = generate_minutes(cfg, renamed, hotwords=hotwords,
                                    meeting_time=meeting_time, base_start_ms=base_ms)

    # 3. 导出：总结信息图（best-effort）+ docx/md 同名成对 + PDF
    out_dir = _out_dir(cfg)
    base = next_base_name(out_dir)
    markdown, image_path = _build_summary_image(cfg, markdown, base)
    docx_path, md_path = save_minutes_pair(markdown, out_dir, base_name=base)
    pdf_path = _export_pdf(docx_path)

    if progress:
        progress("done", "")
    log.info("[完成] 总耗时 %.1f 秒，纪要：%s（md：%s；pdf：%s；信息图：%s）",
             time.monotonic() - t0, docx_path, md_path, pdf_path or "未生成",
             image_path or "未生成")
    return docx_path, {"mapping": mapping, "utterance_count": len(utterances),
                       "md_path": md_path, "pdf_path": pdf_path,
                       "image_path": image_path}


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

    hotwords = load_hotwords()
    print(f"[测试] 热词 {len(hotwords)} 个：{'、'.join(hotwords) or '（空表，不带 corpus）'}",
          flush=True)
    session = MeetingStreamSession(cfg, hotwords=hotwords)
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
        description="会议记录管线。CLI 默认即文件识别路径（备用），"
                    "流式真实链路验收用 --test-stream-wav。")
    parser.add_argument("wav", nargs="?", help="录音 wav 文件（文件识别路径用）")
    parser.add_argument("--test-stream-wav", metavar="WAV",
                        help="流式真实链路验收：读 wav 按 200ms 实时喂给流式会话，"
                             "打印每个 definite 分句")
    parser.add_argument("--mock-asr", metavar="JSON", help="识别环节读本地假识别结果（测试用）")
    parser.add_argument("--audio-url", metavar="URL", help="跳过 TOS 上传，直接用公网音频 URL（调试用）")
    parser.add_argument("--format", dest="fmt", help="audio-url 模式下指定音频格式（如 wav）")
    parser.add_argument("--meeting-time", metavar="TIME",
                        help="开会时间（如 2026-09-16 15:46）。默认从 wav/jsonl 文件名解析，"
                             "解析不到写「未记录」")
    args = parser.parse_args(argv)

    # 输入源（wav / --mock-asr / --audio-url）至少给一个，否则会一路走到识别深处
    # 才炸 TypeError；这里提前用中文用法提示拦住（退出码 2 = 用法错误）
    if not (args.test_stream_wav or args.wav or args.mock_asr or args.audio_url):
        print("失败：没有指定输入音频。用法：\n"
              "  python pipeline.py <wav文件>                              # 文件识别路径\n"
              "  python pipeline.py --mock-asr tests/fake_asr_result.json  # 用本地假识别结果\n"
              "  python pipeline.py --audio-url <公网音频URL> [--format wav]  # 跳过 TOS 上传\n"
              "  python pipeline.py --test-stream-wav <wav文件>            # 流式真实链路验收",
              file=sys.stderr)
        return 2

    if args.test_stream_wav:
        try:
            return _test_stream_wav(args.test_stream_wav)
        except (ConfigError, RuntimeError, OSError, TypeError, ValueError) as e:
            print(f"\n失败：{e}", file=sys.stderr)
            return 1

    meeting_time = args.meeting_time or meeting_time_from_text(
        args.wav or args.mock_asr or args.audio_url or "")
    hotwords = load_hotwords()
    print(f"  会议时间：{meeting_time or '未记录'}；热词：{len(hotwords)} 个", flush=True)
    try:
        path, report = run_pipeline(
            wav_path=args.wav,
            progress=lambda s, d: print(f"  [{STATE_TEXT[s]}] {d}".rstrip(), flush=True),
            mock_asr_file=args.mock_asr,
            audio_url=args.audio_url,
            audio_fmt=args.fmt,
            meeting_time=meeting_time,
            hotwords=hotwords,
        )
    # ValueError 兜住 --mock-asr 指向非 JSON 文件（JSONDecodeError 是它的子类）、
    # TypeError 兜住参数类型不对造成的深处异常，不让英文栈喷到命令行
    except (ConfigError, RuntimeError, OSError, TypeError, ValueError) as e:
        print(f"\n失败：{e}", file=sys.stderr)
        return 1
    print(f"\n已生成 Word：{path}")
    print(f"已生成 Markdown：{report['md_path']}")
    if report.get("pdf_path"):
        print(f"已生成 PDF：{report['pdf_path']}")
    else:
        print("PDF 这次没生成（本机没装 WPS/Word 或转换失败）——Word 和 md 不受影响")
    if report.get("image_path"):
        print(f"已生成总结信息图：{report['image_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
