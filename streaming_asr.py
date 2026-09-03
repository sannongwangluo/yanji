# -*- coding: utf-8 -*-
"""火山豆包流式语音识别（双向流式 bigmodel_async）：长连接、边录边出字。

协议（自定义二进制帧，大端；参考官方《接入语音模型》demo 帧结构独立重写）：
- 端点 wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async（双向流式优化版，
  支持说话人分离 ssd）；
- 鉴权 HTTP header：X-Api-Key（新版豆包语音控制台 UUID key）/ X-Api-Resource-Id
  volc.seedasr.sauc.duration（流式 2.0 小时版）/ X-Api-Request-Id / X-Api-Connect-Id
  （随机 UUID）/ X-Api-Sequence: -1；
- 二进制帧（大端）：4 字节头（version<<4|header_size / message_type<<4|flags /
  serialization<<4|compression / 0x00）+ 可选 4 字节 sequence + 4 字节 payload size
  + gzip 压缩 payload。message_type：0b0001=full client request（JSON）、
  0b0010=audio only、0b1001=server full response、0b1111=error；
  flags：0b0001=正 seq、0b0011=客户端负 seq（最后一包）、0b0010=服务端收尾帧位；
- full client request 的 JSON：request 段 model_name="bigmodel"、
  enable_nonstream=true（二遍识别：实时逐字 + nostream 复核，definite=true 的分句
  才是准的）、enable_speaker_info=true、ssd_version="200"、enable_punc/enable_itn、
  show_utterances=true、result_type="single"（增量返回）；
- 音频 200ms 一包（16k s16le mono = 6400 字节/包），gzip 压缩；散会发负 seq 空包；
- 服务端每包回 full server response，本模块只取 definite=true 的 utterance。
  说话人字段实测（2026-09-02）为 additions.speaker_id（字符串）；_speaker_of 仍
  兼容 speaker/speaker_id/spk/additions 多路以防版本差异。

设计决策（改前先读项目 AGENTS.md）：
- 网络层用 websockets 库（sync client）：默认不读系统代理 = 强制直连（本机有
  系统代理残留踩坑史：代理内核停了但系统代理还开着时，读系统代理的库会
  WinError 10061；火山域名国内直连即可）；
- reader / sender 两个线程：reader 收响应 + 断线重连，sender 从音频缓冲队列
  取帧发送。feed() 只做字节转换 + 入队，绝不阻塞、绝不抛异常（它在 PortAudio
  录音回调线程里被调用）；
- 断线重连：reader 检测连接断开且未 finish → **先清空音频队列**，再指数退避重连
  （1s 起 ×2、封顶 16s、最多 reconnect_max 次）；重连期间 sender 暂停消费音频
  队列（会议音频照常入队，deque maxlen 50 包 ≈10s 兜底），重建成功后**退避期间
  积累的音频（≤10s）补发给新会话**（漏得更少）；成功后 log 警告「连接已重建，
  说话人标签可能漂移」（新连接是新分离会话，ssd 标签可能复用旧编号但指向不同的人）；
- 防崩落盘：每个 definite 分句追加写 日志/transcript_YYYYMMDD_HHMM.jsonl
  （一行一句、写后 flush），崩机最多丢最后一两句。
"""
import collections
import gzip
import json
import logging
import os
import struct
import threading
import time
import uuid

import numpy as np

from config_loader import ConfigError, app_base_dir

log = logging.getLogger("会议记录")

BASE_DIR = app_base_dir()

# ---- 协议常量 ----
PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0x01              # 1 word = 4 字节

MSG_FULL_REQUEST = 0b0001       # client full request（JSON）
MSG_AUDIO_ONLY = 0b0010         # client audio only
MSG_SERVER_FULL_RESPONSE = 0b1001
MSG_SERVER_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POS_SEQ = 0b0001           # 正 seq
FLAG_LAST = 0b0010              # 服务端最后一包位
FLAG_NEG_WITH_SEQ = 0b0011      # 客户端负 seq（最后一包）

SERIALIZATION_NONE = 0b0000
SERIALIZATION_JSON = 0b0001

COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

DEFAULT_UID = "meeting-minutes"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_MS = 200
_CHUNK_BYTES = DEFAULT_SAMPLE_RATE * 2 * DEFAULT_CHUNK_MS // 1000   # 6400 字节/包
_CHUNK_QUEUE_MAX = 50            # 音频缓冲兜底 50 包 ≈ 10 秒

# 空音频错误码（纯静音包服务端可能返回，非致命）
_EMPTY_AUDIO_CODE = 45000002

DEFAULT_REQUEST = {
    "model_name": "bigmodel",
    "language": "zh-CN",
    "enable_nonstream": True,     # 二遍识别：实时逐字 + nostream 复核
    "enable_speaker_info": True,
    "ssd_version": "200",
    "enable_punc": True,
    "enable_itn": True,
    "show_utterances": True,
    "result_type": "single",      # 增量返回
}

# ---- websockets 依赖（sync client，默认不读系统代理 = 强制直连）----
try:
    from websockets.sync.client import connect as _ws_connect
    _HAS_WEBSOCKETS = True
except Exception:  # pragma: no cover - 依赖缺失时 start() 给中文提示
    _ws_connect = None
    _HAS_WEBSOCKETS = False


# ---- 纯协议函数（单测直接覆盖，不碰网络）----

def build_header(message_type, flags, serialization, compression):
    """4 字节协议头（大端）。

    byte0 = version<<4 | header_size（header_size 单位 4 字节，恒 1）；
    byte1 = message_type<<4 | flags；byte2 = serialization<<4 | compression；byte3 = 0。
    """
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def build_full_request(seq, uid=DEFAULT_UID, sample_rate=DEFAULT_SAMPLE_RATE,
                       request=None):
    """构造 full client request 帧（JSON payload，gzip，正 seq）。"""
    request = DEFAULT_REQUEST if request is None else request
    payload = {
        "user": {"uid": uid},
        "audio": {"format": "pcm", "codec": "raw", "rate": sample_rate,
                  "bits": 16, "channel": 1},
        "request": request,
    }
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return (build_header(MSG_FULL_REQUEST, FLAG_POS_SEQ,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
            + struct.pack(">i", seq)
            + struct.pack(">I", len(body))
            + body)


def build_audio_request(seq, pcm, is_last):
    """构造 audio only 帧（gzip 压缩原始 PCM；最后一包用负 seq 标记）。"""
    flags = FLAG_NEG_WITH_SEQ if is_last else FLAG_POS_SEQ
    seq_val = -seq if is_last else seq
    body = gzip.compress(pcm)
    return (build_header(MSG_AUDIO_ONLY, flags,
                         SERIALIZATION_JSON, COMPRESSION_GZIP)
            + struct.pack(">i", seq_val)
            + struct.pack(">I", len(body))
            + body)


def parse_response(frame):
    """解析服务端二进制帧 → dict 或 None（协议不完整/解压失败返回 None）。"""
    if not frame or len(frame) < 4:
        return None
    header_words = frame[0] & 0x0F
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    offset = header_words * 4
    if len(frame) < offset:
        return None

    seq = None
    if flags & 0x01:
        if len(frame) < offset + 4:
            return None
        seq = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4
    is_last = bool(flags & FLAG_LAST)
    event = None
    if flags & 0x04:
        if len(frame) < offset + 4:
            return None
        event = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4

    code = 0
    if message_type == MSG_SERVER_ERROR:
        if len(frame) < offset + 8:
            return None
        code = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4
        size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
    elif message_type == MSG_SERVER_FULL_RESPONSE:
        if len(frame) < offset + 4:
            return None
        size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
    else:
        return {"message_type": message_type, "is_last": is_last, "code": code,
                "data": None, "seq": seq, "event": event}

    payload = frame[offset:offset + size]
    if compression == COMPRESSION_GZIP and payload:
        try:
            payload = gzip.decompress(payload)
        except Exception:
            payload = b""
    data = None
    if payload and serialization == SERIALIZATION_JSON:
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            data = None
    return {"message_type": message_type, "is_last": is_last, "code": code,
            "data": data, "seq": seq, "event": event}


def frame_to_pcm_bytes(frame_float32):
    """float32 单声道 PCM（[-1,1]）→ 16-bit 小端字节（越界截断）。"""
    a = np.asarray(frame_float32, dtype=np.float32).ravel()
    if a.size == 0:
        return b""
    a = np.clip(a, -1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


def _speaker_of(u):
    """utterance 的说话人标签。实测字段名：additions.speaker_id（2026-09-02），
    仍按 speaker/speaker_id/spk/additions.speaker/additions.speaker_id 顺序兼容；
    都没有归「0」（下游显示为 说话人0）。"""
    for key in ("speaker", "speaker_id", "spk"):
        v = u.get(key)
        if v is not None and str(v).strip():
            return str(v)
    additions = u.get("additions")
    if isinstance(additions, dict):
        for key in ("speaker", "speaker_id"):
            v = additions.get(key)
            if v is not None and str(v).strip():
                return str(v)
    return "0"


def _start_time_of(u):
    """utterance 起时间。实测（2026-09-02）：流式 definite 句没有 utterance 级
    start_time（只有 end_time + 逐字 words），从 words[0].start_time 推导（-1 跳过）。"""
    st = u.get("start_time")
    if st is not None:
        return st
    words = u.get("words")
    if isinstance(words, list):
        for w in words:
            if isinstance(w, dict) and w.get("start_time", -1) >= 0:
                return w["start_time"]
    return None


def definite_utterances(data):
    """服务端 full response JSON → definite=true 的分句列表（增量、只取定稿句）。

    实测确认（2026-09-02）：说话人标签在 additions.speaker_id（字符串）；分句无
    utterance 级 start_time，从逐字 words 推导。返回
    [{speaker, text, start_time, end_time}, ...]，与文件识别路径
    asr_client.parse_utterances 的输出同构，下游映射/纪要逻辑可直接复用。
    """
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    utts = result.get("utterances")
    if not isinstance(utts, list):
        return []
    out = []
    for u in utts:
        if not isinstance(u, dict):
            continue
        text = (u.get("text") or "").strip()
        if not text:
            continue
        definite = u.get("definite")
        additions = u.get("additions")
        if definite is None and isinstance(additions, dict):
            definite = additions.get("definite")
        if not (definite is True or str(definite).lower() == "true"):
            continue
        out.append({
            "speaker": _speaker_of(u),
            "text": text,
            "start_time": _start_time_of(u),
            "end_time": u.get("end_time"),
        })
    return out


def _utterance_id(u):
    """分句去重键：优先 utterance_id（顶层/additions），否则 start_time，再否则文本。"""
    uid = u.get("utterance_id")
    additions = u.get("additions")
    if uid is None and isinstance(additions, dict):
        uid = additions.get("utterance_id")
    if uid is not None:
        return f"id:{uid}"
    if u.get("start_time") is not None:
        return f"t:{u['start_time']}"
    return f"txt:{u.get('text') or ''}"


def filter_new_utterances(utts, seen):
    """按去重键过滤已见过的分句（流式会重复下发同一句的修订版）。"""
    new = []
    for u in utts:
        uid = _utterance_id(u)
        if uid in seen:
            continue
        seen.add(uid)
        new.append(u)
    return new, seen


class MeetingStreamSession:
    """流式识别会话：start() 建连 → feed() 喂音频 → finish() 收尾。

    - 内部两个线程：reader（收响应/断线重连）、sender（从缓冲队列发音频帧）；
    - feed() 只做字节转换 + 入队，绝不阻塞、绝不抛异常（录音回调线程安全）；
    - 每个 definite 分句：追加写 jsonl + 回调 on_utterance(u)（在 reader 线程里）；
    - 断线重连：检测到断开即清空音频缓冲，再指数退避最多 reconnect_max 次；
      退避期间积累的音频（deque 上限 50 包 ≈10s）在重建后补发给新会话，
      重发 full client request、seq 重新计数。
    """

    def __init__(self, cfg, on_utterance=None, transcript_dir=None):
        self._volc = cfg["volc"]
        self._stream = cfg["streaming"]
        self._asr = cfg["asr"]
        self._on_utterance = on_utterance
        self._transcript_dir = transcript_dir  # None → 项目 日志/ 目录（测试可注入临时目录）

        self._ws = None
        self._reader = None
        self._sender = None
        self._cond = threading.Condition()
        self._audio_buf = collections.deque(maxlen=_CHUNK_QUEUE_MAX)
        self._acc = []            # feed 累计余量（不足 200ms 的零头）
        self._acc_n = 0
        self._seq = 0
        self._send_lock = threading.Lock()

        self._connected = False
        self._started = False
        self._failed = False
        self._finishing = threading.Event()
        self._stop = threading.Event()
        self._last_error = ""

        self._utterances = []
        self._seen = set()
        self._utterance_frame_logged = False
        self._jsonl_path = None
        self._jsonl_file = None

    # ---- 状态（供 GUI 状态行轮询）----
    @property
    def connected(self):
        return self._connected

    @property
    def started(self):
        return self._started

    @property
    def failed(self):
        return self._failed

    @property
    def utterances(self):
        return list(self._utterances)

    @property
    def utterance_count(self):
        return len(self._utterances)

    @property
    def jsonl_path(self):
        return self._jsonl_path

    @property
    def last_error(self):
        return self._last_error

    # ---- 生命周期 ----
    def start(self, on_utterance=None):
        """建连 + 发 full request + 起 reader/sender 线程。连不上抛中文异常。"""
        if self._started:
            raise RuntimeError("流式会话已启动，不能重复 start。")
        if not _HAS_WEBSOCKETS:
            raise RuntimeError(
                "缺少 websockets 依赖。请在项目目录执行：\n"
                "  .venv\\Scripts\\python -m pip install -r requirements.txt")
        if on_utterance is not None:
            self._on_utterance = on_utterance
        self._check_credentials()
        self._open_jsonl()
        try:
            self._connect()
        except Exception as e:
            self._close_jsonl()
            try:
                if self._jsonl_path and os.path.getsize(self._jsonl_path) == 0:
                    os.remove(self._jsonl_path)
            except OSError:
                pass
            raise RuntimeError(
                f"连接火山流式识别失败：{e}\n"
                "请检查：1) config.toml 的 [volc] api_key 是否有效；2) 豆包语音控制台"
                "是否已开通「流式语音识别 2.0（时长版）」（开通页 "
                "console.volcengine.com/speech/new/ ）。") from e
        self._started = True
        self._set_connected(True)
        self._reader = threading.Thread(target=self._reader_loop,
                                        name="stream-reader", daemon=True)
        self._sender = threading.Thread(target=self._sender_loop,
                                        name="stream-sender", daemon=True)
        self._reader.start()
        self._sender.start()
        log.info("[流式] 已连接（resource=%s），实时转写落盘 %s",
                 self._stream["resource_id"], self._jsonl_path)

    def feed(self, frame_float32):
        """喂一段 float32 音频（任意长度，录音回调线程调用）。攒够 200ms 才入队。"""
        if self._stop.is_set() or self._finishing.is_set():
            return
        try:
            pcm = frame_to_pcm_bytes(frame_float32)
            if not pcm:
                return
            self._acc.append(pcm)
            self._acc_n += len(pcm)
            if self._acc_n >= _CHUNK_BYTES:
                data = b"".join(self._acc)
                with self._cond:
                    while len(data) >= _CHUNK_BYTES:
                        self._audio_buf.append(data[:_CHUNK_BYTES])
                        data = data[_CHUNK_BYTES:]
                    self._acc = [data] if data else []
                    self._acc_n = len(data)
                    self._cond.notify()
        except Exception:
            log.exception("[流式] feed 异常（不影响录音）")

    def finish(self):
        """散会收尾：等缓冲发完 → 发负 seq 空包 → 读收尾帧 → 关连关文件。不抛异常。"""
        if not self._started:
            self._close_jsonl()
            return
        self._finishing.set()
        # 1. 等 sender 把已入队的音频发完（recorder 已 stop，最多再等 3 秒）
        deadline = time.monotonic() + 3.0
        with self._cond:
            while self._audio_buf and time.monotonic() < deadline:
                self._cond.wait(0.05)
        # 2. 发最后一包（负 seq 空包），告诉服务端音频结束
        try:
            if self._connected and self._ws is not None:
                with self._send_lock:
                    self._seq += 1
                    self._ws.send(build_audio_request(self._seq, b"", is_last=True))
        except Exception as e:
            log.warning("[流式] 发送收尾包失败（不影响已识别内容）：%s", e)
        # 3. 等 reader 读到收尾帧退出（服务端会回最后一帧）
        if self._reader is not None:
            self._reader.join(timeout=12)
        # 4. 停 sender、关连、关文件
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._sender is not None:
            self._sender.join(timeout=5)
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._close_jsonl()
        log.info("[流式] 会话结束：共 %d 句 definite 分句", len(self._utterances))

    # ---- 连接 ----
    def _check_credentials(self):
        if not self._volc["api_key"]:
            raise ConfigError(
                "还没有配置豆包语音的 API Key。\n"
                "流式识别需要它：请按 README「首次配置」第 1 步，在火山豆包语音"
                "控制台创建 API Key，填到 config.toml 的 [volc] api_key 一项"
                "（或设置环境变量 VOLC_API_KEY）。")

    def _request_params(self):
        """流式 full request 的 request 段参数（二遍识别 + 说话人分离 ssd200）。"""
        return {
            "model_name": self._stream["model_name"],
            "language": self._asr["language"],
            "enable_nonstream": self._stream["enable_nonstream"],
            "enable_speaker_info": self._asr["enable_speaker_info"],
            "ssd_version": self._stream["ssd_version"],
            "enable_punc": self._asr["enable_punc"],
            "enable_itn": self._asr["enable_itn"],
            "show_utterances": self._asr["show_utterances"],
            "result_type": "single",
        }

    def _connect(self):
        """建连并发送 full client request（每次连接 seq 重新从 1 起）。阻塞。"""
        ws = _ws_connect(
            self._stream["url"],
            additional_headers={
                "X-Api-Key": self._volc["api_key"],
                "X-Api-Resource-Id": self._stream["resource_id"],
                "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Api-Connect-Id": str(uuid.uuid4()),
                "X-Api-Sequence": "-1",
            },
            open_timeout=10,
            close_timeout=5,
            max_queue=None,
        )
        self._ws = ws
        self._seq = 1
        ws.send(build_full_request(self._seq, uid=DEFAULT_UID,
                                   request=self._request_params()))

    def _close_ws(self):
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _set_connected(self, value):
        with self._cond:
            self._connected = value
            self._cond.notify_all()

    # ---- reader / sender 线程 ----
    def _reader_loop(self):
        last_frame = time.monotonic()
        while not self._stop.is_set():
            if self._finishing.is_set() and time.monotonic() - last_frame > 6.0:
                break  # 收尾帧等了 6 秒还没来，撤
            ws = self._ws
            if ws is None:
                if self._finishing.is_set() or self._stop.is_set():
                    break
                if not self._reconnect():
                    break
                continue
            try:
                frame = ws.recv(timeout=0.5)
            except TimeoutError:
                continue
            except Exception as e:
                log.debug("[流式] 读响应异常：%s", e)
                if self._finishing.is_set() or self._stop.is_set():
                    break
                if not self._reconnect():
                    break
                continue
            last_frame = time.monotonic()
            if isinstance(frame, str):
                continue  # 忽略文本帧
            resp = parse_response(frame)
            if resp is None:
                continue
            if resp["code"] != 0:
                if resp["code"] == _EMPTY_AUDIO_CODE:
                    log.debug("[流式] 空音频提示 %d（正常）", resp["code"])
                else:
                    log.warning("[流式] 服务端错误码 %d：%r", resp["code"], resp["data"])
                continue
            is_last = resp["is_last"]
            if resp["data"]:
                is_last = self._handle_data(resp["data"]) or is_last
            if is_last:
                if self._finishing.is_set():
                    break
                # 服务端主动发收尾帧但会议还没散 → 连接被掐，重连
                if not self._reconnect():
                    break

    def _sender_loop(self):
        while not self._stop.is_set():
            with self._cond:
                while (not self._audio_buf or not self._connected) \
                        and not self._stop.is_set():
                    self._cond.wait(0.2)
                if self._stop.is_set():
                    break
                pcm = self._audio_buf.popleft()
            try:
                with self._send_lock:
                    self._seq += 1
                    self._ws.send(build_audio_request(self._seq, pcm, is_last=False))
            except Exception as e:
                log.debug("[流式] 发送音频帧失败：%s（reader 会负责重连）", e)

    def _reconnect(self):
        """reader 检测到断开后调用：先清空音频缓冲，再指数退避重连
        （最多 reconnect_max 次）。

        清空发生在检测到断开时（不是重建成功后）；退避期间积累的音频
        （deque 上限 50 包 ≈10s）会在重建后由 sender 补发给新会话。返回是否恢复。
        """
        if self._finishing.is_set() or self._stop.is_set():
            return False
        self._set_connected(False)
        self._close_ws()
        with self._cond:
            self._audio_buf.clear()   # 断开即清空；退避期间积累的音频会在重建后补发
        max_attempts = self._stream["reconnect_max"]
        delay = 1.0
        for attempt in range(1, max_attempts + 1):
            log.warning("[流式] 连接断开，%.0f 秒后第 %d/%d 次重连…",
                        delay, attempt, max_attempts)
            if self._finishing.wait(delay) or self._stop.is_set():
                return False
            try:
                self._connect()
            except Exception as e:
                log.warning("[流式] 第 %d 次重连失败：%s", attempt, e)
                delay = min(delay * 2, 16.0)
                continue
            log.warning("[流式] 连接已重建，说话人标签可能漂移（新会话的 ssd 标签可能复用旧编号）")
            self._set_connected(True)
            return True
        self._failed = True
        self._last_error = f"重连 {max_attempts} 次全部失败"
        log.error("[流式] 重连 %d 次全部失败，流式识别停止（录音不受影响）", max_attempts)
        return False

    # ---- 响应处理 / 落盘 ----
    def _handle_data(self, data):
        """解析一帧 full response：抽 definite 分句 → 落盘 → 回调。返回 JSON 层收尾标志。"""
        raw_utts = definite_utterances(data)
        if raw_utts and not self._utterance_frame_logged:
            self._utterance_frame_logged = True
            log.info("[流式] 首个产出 definite 分句的原始 JSON（确认 speaker 字段名用）：%s",
                     json.dumps(data, ensure_ascii=False)[:2500])
        new_utts = filter_new_utterances(raw_utts, self._seen)[0]
        for u in new_utts:
            self._utterances.append(u)
            self._append_jsonl(u)
            if self._on_utterance is not None:
                try:
                    self._on_utterance(u)
                except Exception:
                    log.exception("[流式] on_utterance 回调异常（不影响继续识别）")
        if new_utts:
            log.info("[流式] +%d 句（累计 %d）：%s", len(new_utts),
                     len(self._utterances),
                     " | ".join(u["text"][:40] for u in new_utts))
        return bool(data.get("is_last_package"))

    def _open_jsonl(self):
        log_dir = self._transcript_dir or os.path.join(BASE_DIR, "日志")
        os.makedirs(log_dir, exist_ok=True)
        self._jsonl_path = os.path.join(
            log_dir, time.strftime("transcript_%Y%m%d_%H%M.jsonl"))
        self._jsonl_file = open(self._jsonl_path, "a", encoding="utf-8")

    def _append_jsonl(self, u):
        if self._jsonl_file is None:
            return
        try:
            self._jsonl_file.write(json.dumps(u, ensure_ascii=False) + "\n")
            self._jsonl_file.flush()
        except Exception:
            log.exception("[流式] 转写落盘失败（识别继续）")

    def _close_jsonl(self):
        if self._jsonl_file is not None:
            try:
                self._jsonl_file.close()
            except Exception:
                pass
            self._jsonl_file = None
