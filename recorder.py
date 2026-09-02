# -*- coding: utf-8 -*-
"""录音：sounddevice 录 16k 单声道 float32，流式写 16-bit PCM WAV 到磁盘。

写法参考 voice-input 工具（同为 16k/单声道/float32 回调）后独立重写。
一小时的会约 115MB，边录边写盘，不占内存；回调里只写盘和推电平，不做重活。
"""
import os
import queue
import time
import wave

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000


class RecorderError(Exception):
    """录音相关错误，message 是中文，可直接展示给用户。"""


class Recorder:
    def __init__(self):
        self._stream = None
        self._wav = None
        self._started_at = 0.0
        self._frames_written = 0
        self._on_frame = None
        self.level_queue = queue.Queue(maxsize=8)  # rms 采样（0~1），供 GUI 电平条

    # ---- 状态 ----
    @property
    def recording(self):
        return self._stream is not None

    @property
    def elapsed(self):
        if not self.recording:
            return 0.0
        return time.monotonic() - self._started_at

    # ---- 录音 ----
    def start(self, wav_path, on_frame=None):
        """开始录音。on_frame(frame_float32)：可选回调，每块音频都调（流式识别用）。

        回调在 PortAudio 录音线程里执行，必须快、不能阻塞（内部异常会吞掉）。
        """
        if self.recording:
            raise RecorderError("已经在录音了，先点「结束并生成纪要」。")
        os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
        self._on_frame = on_frame
        self._wav = wave.open(wav_path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)
        self._frames_written = 0
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as e:
            self._wav.close()
            self._wav = None
            raise RecorderError(
                f"打开麦克风失败：{e}\n"
                "请检查：1) 电脑有没有接麦克风/全向麦；2) 系统设置→隐私→麦克风，"
                "是否允许本程序使用麦克风。") from e
        self._started_at = time.monotonic()

    def _on_audio(self, indata, frames, time_info, status):
        """PortAudio 回调线程里执行：转 16-bit PCM 写盘 + 推送电平 + 喂流式会话。"""
        try:
            pcm = np.clip(indata, -1.0, 1.0)
            pcm = (pcm * 32767.0).astype("<i2")
            self._wav.writeframes(pcm.tobytes())
            self._frames_written += frames
            rms = float(np.sqrt(np.mean(indata ** 2)))
            try:
                self.level_queue.put_nowait(rms)
            except queue.Full:
                pass  # GUI 没来得及读就丢，不影响录音
            if self._on_frame is not None:
                try:
                    self._on_frame(indata)
                except Exception:
                    pass  # 流式喂帧失败不影响录音
        except Exception:
            pass  # 回调里绝不把异常抛给 PortAudio

    def stop(self):
        """结束录音并返回时长（秒）。录音太短（<1s）视为无效。"""
        if not self.recording:
            raise RecorderError("当前没有在录音。")
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._wav.close()
        self._wav = None
        duration = self._frames_written / SAMPLE_RATE
        if duration < 1.0:
            raise RecorderError(f"录音太短（{duration:.1f} 秒），没有内容可生成纪要。")
        return duration
