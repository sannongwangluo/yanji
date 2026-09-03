# -*- coding: utf-8 -*-
"""配置加载：config.toml 优先，环境变量兜底，内置默认收尾。

优先级（高→低）：config.toml 里的值 → 环境变量 → 默认值。
缺凭证由各模块抛 ConfigError（中文大白话，可直接展示给最终用户，不带栈 trace）。

tomllib 只读，不提供写回；配置项的持久化由 save_config_value() 手写回
config.toml（按节定位、只动目标一行，其余原样保留）；save_output_dir 是它的
[app] output_dir 特例。
"""
import os
import re
import sys
import tomllib


def app_base_dir():
    """程序数据根目录：config.toml / 录音 / 输出 / 日志 都落在这里。

    PyInstaller 单文件 exe 下 `__file__` 指向临时解压目录（_MEIPASS，退出即删），
    必须改用 exe 所在目录（sys.executable），否则配置和产物会写到临时目录而丢失。
    普通 `python xxx.py` 运行时 sys.frozen 不存在，仍用源码目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = app_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.toml")


class ConfigError(Exception):
    """配置/凭证缺失或错误。message 是中文提示，直接展示给用户看。"""


def _first(*values):
    """取第一个非空字符串值。"""
    for v in values:
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _as_bool(v, default):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


def _as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def load_config(config_path=None):
    """读配置并合并环境变量。返回嵌套 dict（见 config.example.toml 的键名）。"""
    path = config_path or CONFIG_PATH
    toml_cfg = {}
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                toml_cfg = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"读 config.toml 失败：{e}") from e

    def table(name):
        t = toml_cfg.get(name, {})
        return t if isinstance(t, dict) else {}

    volc = table("volc")
    tos = table("tos")
    stream = table("streaming")
    ds = table("deepseek")
    asr_t = table("asr")
    app = table("app")

    return {
        "volc": {
            # 语音识别 API Key：新版豆包语音控制台签发（UUID 格式），流式/文件识别共用
            "api_key": _first(volc.get("api_key"),
                              os.environ.get("VOLC_API_KEY"),
                              os.environ.get("VOLCENGINE_API_KEY")),
            # 资源 ID：豆包录音文件识别模型 2.0（标准版，非闲时）——仅 --file-mode 用
            "resource_id": _first(volc.get("resource_id"), "volc.seedasr.auc"),
        },
        "streaming": {
            # 长连接流式识别（主路径，默认）
            "url": _first(stream.get("url"),
                          "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"),
            # 资源 ID：流式 2.0 小时版（说话人分离）
            "resource_id": _first(stream.get("resource_id"),
                                  "volc.seedasr.sauc.duration"),
            "model_name": _first(stream.get("model_name"), "bigmodel"),
            "ssd_version": _first(stream.get("ssd_version"), "200"),
            # 二遍识别：实时逐字 + nostream 复核（definite=true 的分句才准）
            "enable_nonstream": _as_bool(stream.get("enable_nonstream"), True),
            "chunk_ms": _as_int(stream.get("chunk_ms"), 200),
            "reconnect_max": _as_int(stream.get("reconnect_max"), 5),
        },
        "tos": {
            "access_key_id": _first(tos.get("access_key_id"),
                                    os.environ.get("VOLCENGINE_ACCESS_KEY_ID")),
            "secret_access_key": _first(tos.get("secret_access_key"),
                                        os.environ.get("VOLCENGINE_SECRET_ACCESS_KEY")),
            "bucket": _first(tos.get("bucket"),
                             os.environ.get("VOLCENGINE_TOS_BUCKET")),
            "region": _first(tos.get("region"),
                             os.environ.get("VOLCENGINE_TOS_REGION"), "cn-beijing"),
            # 留空则按 region 自动生成 tos-<region>.volces.com
            "endpoint": _first(tos.get("endpoint"), ""),
        },
        "deepseek": {
            "api_key": _first(ds.get("api_key"), os.environ.get("DEEPSEEK_API_KEY")),
            "model": _first(ds.get("model"), "deepseek-v4-flash"),
            "endpoint": _first(ds.get("endpoint"),
                               "https://api.deepseek.com/v1/chat/completions"),
        },
        "asr": {
            "language": _first(asr_t.get("language"), "zh-CN"),
            "enable_speaker_info": _as_bool(asr_t.get("enable_speaker_info"), True),
            "show_utterances": _as_bool(asr_t.get("show_utterances"), True),
            "enable_punc": _as_bool(asr_t.get("enable_punc"), True),
            "enable_itn": _as_bool(asr_t.get("enable_itn"), True),
            "enable_ddc": _as_bool(asr_t.get("enable_ddc"), True),
            "poll_interval_sec": _as_int(asr_t.get("poll_interval_sec"), 5),
            "poll_timeout_sec": _as_int(asr_t.get("poll_timeout_sec"), 1500),
        },
        "app": {
            # 纪要是否自动写入第二大脑（个人记忆库）。朋友试用必须保持 false。
            "ingest_brain_memory": _as_bool(app.get("ingest_brain_memory"), False),
            # 识别完成后是否自动从 TOS 删除音频（隐私，建议保持 true）
            "delete_audio_after_asr": _as_bool(app.get("delete_audio_after_asr"), True),
            "brain_memory_dir": _first(app.get("brain_memory_dir"),
                                       r"D:\Github 资料\5.0 类脑记忆"),
            # 纪要 docx 输出目录：留空 = 程序目录下「输出」文件夹（pipeline._out_dir 统一回落）
            "output_dir": _first(app.get("output_dir")),
        },
    }


def _toml_str(value):
    """值 → TOML 字符串。默认 literal string（单引号，Windows 反斜杠路径不用
    转义）；值含单引号时回退 basic string（双引号 + 反斜杠/双引号转义）。"""
    if "'" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return f"'{value}'"


def save_config_value(section, key, value, config_path=CONFIG_PATH):
    """把 config.toml 某一节里的单个键值写回（tomllib 只读，手写"只动一行"）。

    - 已存在该 key 的行：整行替换（行尾原样保留）；
    - 节存在但没有该 key：插到节标题行之后；
    - 连节都没有：文件末尾追加节标题 + 该行（防御，正常 config 各节必有）。
    值用 _toml_str 写成 TOML 字符串；只动目标一行，其余内容原样保留。
    config.toml 不存在/不可写时抛 OSError，由调用方（GUI）用中文提示。
    """
    with open(config_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    section_title = f"[{section}]"
    sec_idx = next((i for i, l in enumerate(lines)
                    if l.strip() == section_title), None)
    key_re = re.compile(rf"^{re.escape(key)}\s*=")
    key_idx = None
    if sec_idx is not None:
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                break  # 已到下一个节，本节里没有该 key
            if key_re.match(stripped):
                key_idx = i
                break

    new_line = f"{key} = {_toml_str(value)}"
    if key_idx is not None:
        eol = "\r\n" if lines[key_idx].endswith("\r\n") else "\n"
        lines[key_idx] = new_line + eol
    elif sec_idx is not None:
        eol = "\r\n" if lines[sec_idx].endswith("\r\n") else "\n"
        lines.insert(sec_idx + 1, new_line + eol)
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.extend([f"{section_title}\n", new_line + "\n"])

    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def save_output_dir(path, config_path=CONFIG_PATH):
    """把纪要输出目录写回 config.toml 的 [app] output_dir（save_config_value 特例）。"""
    save_config_value("app", "output_dir", path, config_path=config_path)
