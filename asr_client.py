# -*- coding: utf-8 -*-
"""豆包录音文件识别 2.0（标准版）客户端。

官方流程（https://www.volcengine.com/docs/6561/1354868）：
  1. 音频上传到火山对象存储 TOS（预签名 PUT），拿下载链接；
  2. POST submit 提交识别任务（header：X-Api-Key / X-Api-Resource-Id / X-Api-Request-Id）；
  3. POST query 轮询结果（header X-Api-Status-Code：20000000=完成，20000001/2=处理中）；
  4. 识别完成后（默认）从 TOS 删除音频——隐私：识别完建议删除。

鉴权：新版豆包语音控制台签发的 API Key（UUID 格式），走 X-Api-Key 头。
纯 HTTP + requests，无 websocket、无 SDK；TOS 用 TOS4-HMAC-SHA256 预签名，不引第三方库。
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

from config_loader import ConfigError

log = logging.getLogger("会议记录")

SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"

# 官方错误码表 + 社区踩坑，翻译成大白话提示
ERROR_HINTS = {
    "20000003": "音频里没有检测到人声（可能麦克风没录上、或全程静音）。",
    "45000001": "请求参数有问题，请检查 config.toml 里的语音配置。",
    "45000002": "音频是空的，请确认录音文件正常。",
    "45000131": "提交太频繁，稍等几分钟再试。",
    "45000132": "音频超过 512MB 大小上限。",
    "45000151": "音频格式不对（支持 wav/mp3/m4a/ogg/flac）。",
    "55000031": "火山服务繁忙，请稍后重试。",
    "45000006": "识别服务下载不到音频：通常是 TOS 桶策略只给了上传权限、没给读取权限。"
                "请在桶的「权限管理→存储桶授权策略管理」用「文件夹读写」模板授权（读+写都要）。",
    "45000030": "火山账号还没开通「录音文件识别」：登录新版豆包语音控制台"
                "（https://console.volcengine.com/speech/new/ ）→ 语音识别 → 开通模型 →"
                "开通「录音文件识别2.0」，再回来点「结束并生成纪要」。",
}


def _friendly_api_error(prefix, status, message):
    hint = ERROR_HINTS.get(str(status))
    text = f"{prefix}：{message}（错误码 {status}）"
    return f"{text}\n{hint}" if hint else text


def _tos_sign_url(method, url, ak, sk, region, expires=3600):
    """生成 TOS V4 预签名 URL（TOS4-HMAC-SHA256，查询串签名）。"""
    p = urlparse(url)
    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{date_stamp}/{region}/tos/request"
    q = {
        "X-Tos-Algorithm": "TOS4-HMAC-SHA256",
        "X-Tos-Credential": f"{ak}/{scope}",
        "X-Tos-Date": amz_date,
        "X-Tos-Expires": str(expires),
        "X-Tos-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(q.items()))
    canonical_request = "\n".join([
        method,
        quote(p.path, safe="/"),
        canonical_qs,
        f"host:{p.hostname}",
        "host",
        "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        "TOS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _hmac(sk.encode(), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, "tos")
    key = _hmac(key, "request")
    sig = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{p.scheme}://{p.hostname}{p.path}?{canonical_qs}&X-Tos-Signature={sig}"


class AsrClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.volc = cfg["volc"]
        self.tos = cfg["tos"]
        self.asr_cfg = cfg["asr"]
        # trust_env=False：强制直连，不吃系统代理（避免系统代理残留导致连接失败，
        # 火山这几个域名国内直连即可，走残留代理会 WinError 10061）
        self._sess = requests.Session()
        self._sess.trust_env = False

    # ---- 凭证检查：缺什么给中文大白话，不给栈 ----
    def check_credentials(self, need_tos=True):
        if not self.volc["api_key"]:
            raise ConfigError(
                "还没有配置豆包语音的 API Key。\n"
                "请按项目 README「首次配置」第 1 步，在火山豆包语音控制台创建 API Key，"
                "填到 config.toml 的 [volc] api_key 一项（或设置环境变量 VOLC_API_KEY）。")
        if need_tos:
            missing = []
            if not self.tos["access_key_id"]:
                missing.append("access_key_id（IAM 访问密钥 ID，AKLT 开头）")
            if not self.tos["secret_access_key"]:
                missing.append("secret_access_key（IAM 访问密钥）")
            if not self.tos["bucket"]:
                missing.append("bucket（TOS 存储桶名称）")
            if missing:
                raise ConfigError(
                    "还差火山对象存储 TOS 的配置：\n  - " + "\n  - ".join(missing) +
                    "\n请按 README「首次配置」第 2、3 步开通，并填到 config.toml 的 [tos] 一节。")

    # ---- 上传 ----
    def _object_url(self, fmt):
        """对象地址（未签名）：https://<bucket>.tos-<region>.volces.com/<key>"""
        bucket = self.tos["bucket"]
        region = self.tos["region"]
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,62}$", bucket):
            raise ConfigError(f"config.toml 里的 TOS bucket 名称不合法：{bucket}")
        if not re.match(r"^[a-z0-9-]+$", region):
            raise ConfigError(f"config.toml 里的 TOS region 不合法：{region}")
        endpoint = self.tos.get("endpoint") or f"tos-{region}.volces.com"
        host = f"{bucket}.{endpoint}"
        key = f"huiyi/{time.strftime('%Y%m%d')}/{uuid.uuid4().hex}.{fmt}"
        return f"https://{host}/{key}"

    def _upload(self, filepath, fmt):
        """上传本地音频到 TOS，返回对象 URL（未签名）。带重试。"""
        ak, sk = self.tos["access_key_id"], self.tos["secret_access_key"]
        url = self._object_url(fmt)
        put_url = _tos_sign_url("PUT", url, ak, sk, self.tos["region"], expires=300)
        log.info("[上传] 开始上传 %s（%.1f MB）", filepath,
                 os.path.getsize(filepath) / 1048576)
        last_err = None
        for attempt in range(3):
            try:
                with open(filepath, "rb") as f:
                    resp = self._sess.put(
                        put_url, data=f,
                        headers={"Content-Type": "audio/wav"},
                        timeout=600)
                if resp.status_code in (200, 201):
                    log.info("[上传] 完成")
                    return url
                if resp.status_code == 403:
                    raise ConfigError(
                        "上传音频到 TOS 被拒绝（403）。\n"
                        "最常见原因：桶策略没配好。请在 TOS 桶的「权限管理→存储桶授权策略管理」"
                        "里创建策略，选「文件夹读写」模板授权（读+写都要）。")
                last_err = RuntimeError(
                    f"TOS 上传失败（HTTP {resp.status_code}）：{resp.text[:200]}")
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
            except ConfigError:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("[上传] 第 %d 次失败，%d 秒后重试：%s", attempt + 1, wait, last_err)
            time.sleep(wait)
        raise RuntimeError(f"TOS 上传失败（重试 3 次）：{last_err}") from last_err

    def _delete_object(self, url):
        """识别完成后删音频（隐私）。失败只记日志，不阻断出稿。"""
        try:
            ak, sk = self.tos["access_key_id"], self.tos["secret_access_key"]
            del_url = _tos_sign_url("DELETE", url, ak, sk, self.tos["region"], expires=300)
            self._sess.delete(del_url, timeout=60)
            log.info("[清理] 已从 TOS 删除音频")
        except Exception as e:
            log.warning("[清理] 删除音频失败（可到 TOS 控制台手动删）：%s", e)

    # ---- 提交 / 轮询 ----
    def _headers(self, request_id, with_sequence):
        h = {
            "Content-Type": "application/json",
            "X-Api-Key": self.volc["api_key"],
            "X-Api-Resource-Id": self.volc["resource_id"],
            "X-Api-Request-Id": request_id,
        }
        if with_sequence:
            h["X-Api-Sequence"] = "-1"
        return h

    def _request_body(self, audio_url, fmt):
        r = self.asr_cfg
        body = {
            "user": {"uid": "meeting-minutes"},
            "audio": {"url": audio_url, "format": fmt},
            "request": {
                "model_name": "bigmodel",
                "language": r["language"],
                "enable_itn": r["enable_itn"],
                "enable_punc": r["enable_punc"],
                "enable_ddc": r["enable_ddc"],
                "show_utterances": r["show_utterances"],
                "enable_speaker_info": r["enable_speaker_info"],
            },
        }
        return body

    def _submit(self, audio_url, fmt):
        request_id = str(uuid.uuid4())
        headers = self._headers(request_id, with_sequence=True)
        body = self._request_body(audio_url, fmt)
        last_err = None
        for attempt in range(3):
            try:
                resp = self._sess.post(SUBMIT_URL, headers=headers, json=body, timeout=30)
                status = resp.headers.get("X-Api-Status-Code", "")
                message = resp.headers.get("X-Api-Message", "")
                if status == "20000000":
                    log.info("[识别] 任务已提交，request_id=%s", request_id)
                    return request_id
                if status in ("55000031",):  # 服务繁忙可重试
                    last_err = _friendly_api_error("提交识别任务失败", status, message)
                else:
                    raise RuntimeError(_friendly_api_error("提交识别任务失败", status, message))
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = f"提交识别任务网络失败：{e}"
            wait = 2 ** (attempt + 1)
            log.warning("[识别] 提交第 %d 次失败，%d 秒后重试", attempt + 1, wait)
            time.sleep(wait)
        raise RuntimeError(last_err or "提交识别任务失败")

    def _poll(self, request_id, progress=None):
        """轮询直到完成。progress(detail) 回传等待时长文本。"""
        interval = self.asr_cfg["poll_interval_sec"]
        timeout = self.asr_cfg["poll_timeout_sec"]
        headers = self._headers(request_id, with_sequence=False)
        started = time.monotonic()
        net_errors = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                raise RuntimeError(f"识别超时（等了 {int(timeout / 60)} 分钟还没出结果），请稍后再试。")
            try:
                resp = self._sess.post(QUERY_URL, headers=headers, json={}, timeout=30)
            except (requests.ConnectionError, requests.Timeout):
                net_errors += 1
                if net_errors >= 5:
                    raise RuntimeError("查询识别结果连续网络失败，请检查网络后重试。")
                time.sleep(2 ** net_errors)
                continue
            net_errors = 0
            status = resp.headers.get("X-Api-Status-Code", "")
            if status == "20000000":
                log.info("[识别] 完成，等待 %.0f 秒", elapsed)
                return resp.json()
            if status in ("20000001", "20000002"):
                if progress:
                    m, s = divmod(int(elapsed), 60)
                    progress("recognizing", f"已等待 {m} 分 {s:02d} 秒")
                time.sleep(interval)
                continue
            if status == "20000003":
                return {"result": {"text": "", "utterances": []}}
            message = resp.headers.get("X-Api-Message", "")
            raise RuntimeError(_friendly_api_error("查询识别结果失败", status, message))

    # ---- 对外主入口 ----
    def transcribe(self, wav_path=None, audio_url=None, fmt=None, progress=None):
        """识别音频。本地文件走 TOS 上传；给 audio_url 则跳过上传（调试/网盘直链用）。

        progress(state, detail)：state ∈ uploading / recognizing；detail 给用户看的进度。
        返回识别结果 dict（与官方 query 应答同构）。
        """
        self.check_credentials(need_tos=(audio_url is None))
        if audio_url is None:
            if fmt is None:
                fmt = "wav"
            if progress:
                progress("uploading", "正在上传音频到火山云端…")
            obj_url = self._upload(wav_path, fmt)
            audio_url = _tos_sign_url(
                "GET", obj_url, self.tos["access_key_id"],
                self.tos["secret_access_key"], self.tos["region"], expires=3600)
            cleanup_url = obj_url
        else:
            cleanup_url = None
        if progress:
            progress("recognizing", "正在提交识别任务…")
        request_id = self._submit(audio_url, fmt or "wav")
        data = self._poll(request_id, progress=progress)
        if cleanup_url and self.cfg["app"].get("delete_audio_after_asr", True):
            self._delete_object(cleanup_url)
        return data


def parse_utterances(data):
    """识别应答 → [{speaker, text, start_time, end_time}, ...]（按时间顺序，空句跳过）。

    speaker 取值：utterance.speaker 或 utterance.additions.speaker；都没有则归「0」。
    """
    result = data.get("result") or {}
    utts = result.get("utterances") or []
    out = []
    for u in utts:
        text = (u.get("text") or "").strip()
        if not text:
            continue
        sp = u.get("speaker")
        if sp is None and isinstance(u.get("additions"), dict):
            sp = u["additions"].get("speaker")
        out.append({
            "speaker": str(sp) if sp is not None else "0",
            "text": text,
            "start_time": u.get("start_time"),
            "end_time": u.get("end_time"),
        })
    if not out:
        text = (result.get("text") or "").strip()
        if text:
            out.append({"speaker": "0", "text": text, "start_time": 0, "end_time": None})
    return out
