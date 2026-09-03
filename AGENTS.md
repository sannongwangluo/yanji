# AGENTS.md — 会议记录工具（项目定版记录）

独立项目，不 import 语音输入工具 / realtime-eye 任何代码。改设计先读本文件，翻案要明说。

## 设计定版（2026-09-02 流式架构改版）

- **识别主路径 = 长连接流式**（`streaming_asr.py::MeetingStreamSession`）：端点
  `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`，自定义二进制帧
  （大端 4 字节头/seq/size + gzip JSON）。request 段：`model_name=bigmodel`、
  `enable_nonstream=true`（二遍：实时逐字 + nostream 复核，**只取 definite=true
  的分句**，partial 帧直接丢）、`enable_speaker_info=true`、`ssd_version="200"`、
  `enable_punc/itn`、`show_utterances=true`、`result_type="single"`（增量）。
  音频 200ms 一包（16k s16le mono = 6400B），散会发负 seq 空包收尾。
- **实测协议口径（2026-09-02，--test-stream-wav 验证）**：definite 分句的说话人
  字段是 **`utterance.additions.speaker_id`**（字符串，单人音频实测为 "0"）；
  分句**没有 utterance 级 start_time**（只有 end_time + 逐字 words），起时间从
  `words[0].start_time` 推导（-1 跳过）；没有 utterance_id，去重靠
  start_time→text 兜底链。partial 帧（definite=false）带 `source:"stream"`，
  definite 帧带 `source:"two_pass"`。`_speaker_of`/`_start_time_of` 按此适配。
- **断线重连**：reader 线程检测连接断开且未 finish → **先清空音频缓冲**，再指数
  退避重连（1s 起 ×2、封顶 16s、最多 `[streaming] reconnect_max`=5 次）；重连
  期间 sender 暂停消费音频缓冲（deque maxlen 50 包 ≈10s 兜底，会议音频照常入队），
  重建成功后**退避期间积累的音频（≤10s）补发给新会话**（漏得更少）；成功 log
  警告「连接已重建，说话人标签可能漂移」（新连接是新分离会话，ssd 标签可能复用
  旧编号）。5 次全失败 → session.failed=True，识别停止但录音继续，散会可用
  --file-mode 补出稿。重连/收尾时序有单测 `tests/test_stream_reconnect.py`（假 ws 模拟）。
- **增量落盘防崩**：每句 definite 分句追加写 `日志/transcript_YYYYMMDD_HHMM.jsonl`
  （一行一句、写后 flush）——崩机最多丢最后一两句。会话构造参数
  `transcript_dir` 可注入（测试用临时目录）。
- **报到映射免声纹**：正则 `我是X/我叫X`，按分句顺序首现即定、同标签不覆盖；
  未报到显示「说话人N」。流式实时出字用 `speaker_map.IncrementalSpeakerMap`
  （逐句 update），散会出稿前管线用全量 `_map_speakers` 重算一遍（单一来源，
  终稿口径一致）。
- **文件识别降为备用**（`--file-mode`）：`asr_client.py` 保留不删（TOS 预签名
  上传 + submit/query 轮询），只有流式不可用时才走。TOS 配置可留空。
- **鉴权**：新版豆包语音控制台 API Key（UUID）走 `X-Api-Key` 头 + 流式
  `X-Api-Resource-Id: volc.seedasr.sauc.duration`（流式2.0小时版，已开通）+
  X-Api-Request-Id / X-Api-Connect-Id（UUID）/ X-Api-Sequence: -1。**不是**旧版
  AppID+AccessToken、不是方舟 ARK key（openspeech 会 401 拒收）。
- **网络层强制直连**：流式走 websockets sync client（默认不读系统代理=直连）；
  TOS/轮询走 requests `Session(trust_env=False)`；DeepSeek 走 urllib 空
  ProxyHandler——防系统代理残留 WinError 10061（本机踩坑史）。
- **DeepSeek**：`deepseek-v4-flash`，**不禁 thinking**；超长转写按发言人轮次
  截断 ~30k 字（头部保留，报到都在头部）；已内置自动重试（空内容/网络错误/
  5xx 最多重试 2 次、间隔 3s，401/403 不重试）。
- **docx 排版**只做三档（标题/正文/列表）+ 行内 `**粗体**`；prompt 里已禁表格。
- **ingest_brain_memory 默认 false**（朋友试用必须关）；打开后走第二大脑 CLI
  `add`，失败不阻断出稿。

## 凭证坑（实测沉淀）

- 45000030 `requested resource not granted` = 账号没开通「录音文件识别2.0」
  **（仅 --file-mode 备用路径需要）**。开通页 `console.volcengine.com/speech/new/`，
  开通后有分钟级生效延迟。
- 流式 401 = key 不对（方舟 ARK key 会被拒）或没开通「流式语音识别 2.0
  （时长版）」；GUI/测试模式下报「连接火山流式识别失败」时按此查。
- progress 回调契约统一两参 `progress(state, detail)`；GUI 工作线程只经
  `progress_q`/`utter_q` 回传，**工作线程绝不直接碰 tkinter**。
- TOS 403 / ASR 侧 45000006 = 桶策略缺读权限（仅 file-mode）；境外服务器 TOS
  region 必须海外节点。
- **ssd 说话人分离对低音量/远场语音会分裂新标签**（2026-09-02 三人实测）：
  同一人音量降到 rms≈0.004~0.007 时被打成新 speaker 编号（可可被裂成
  1/3/4 三个标签，其报到句 rms 0.010 vs 老王 0.021）；rms≥0.01 后未再分裂。
  这是火山 ssd 服务端能力边界，我们代码如实使用标签、无 bug。**物理层解决**
  （说话人靠近麦/大声/全向麦），产品层可选「标签合并/改名」暂不做，等朋友
  试用反馈再定。另实测：多人标签 0 基连号（0/1/2…）；断线重连 1 次 1s 自愈，
  重连后标签未漂移（但不保证，漂移风险仍在）。
- 本机现状（2026-09-02）：`VOLC_API_KEY`（UUID，可用）、`DEEPSEEK_API_KEY` 已配；
  **流式 2.0 小时版已开通、真实链路已用 --test-stream-wav 跑通**（test_tts.wav：
  2 句 definite、speaker_id="0"）；TOS 三件套未配（仅 file-mode 需要，不影响主流程）。

## 目录与运行

- 模块：`config_loader` / `recorder` / `streaming_asr`（流式主路径）/
  `asr_client`（file-mode 备用）/ `speaker_map`（全量 + IncrementalSpeakerMap）/
  `minutes_llm` / `docx_writer` / `brain_ingest` / `pipeline`（编排+CLI）/
  `会议记录.py`（tkinter GUI）
- 运行：`.venv\Scripts\python 会议记录.py`；流式验收：
  `python pipeline.py --test-stream-wav <wav>`；备用文件路径：
  `python pipeline.py <wav>`（`--mock-asr` / `--audio-url` 调试用）
- 产物目录：`录音/`（wav，本地备份+file-mode 备用）、`输出/`（docx+md
  同名成对导出，save_minutes_pair）、`日志/`（每天日志 + `transcript_*.jsonl`
  流式逐句落盘）；配置 `config.toml`
  （本地，不分享，.gitignore 已排除）。输出目录可在 GUI 更改（持久化到
  config.toml 的 [app] output_dir，留空=程序目录下「输出」文件夹，
  pipeline._out_dir 统一回落）。
- GUI「设置…」可查看/修改豆包语音与 DeepSeek 两个 API Key（写回 config.toml
  的 [volc]/[deepseek] api_key，留空=走环境变量兜底；保存有格式软校验，
  日志只记长度绝不写明文）。
- 测试：`python -m unittest discover -s tests -v`——54 个用例覆盖 流式二进制帧
  roundtrip / definite 抽取 / speaker 字段兼容 / 增量映射 / 断线重连模拟 /
  DeepSeek 自动重试 / 输出目录与 API Key 配置读写（save_config_value 泛化写回）/
  纪要成对导出（docx+md 同基名）。改动后必跑 py_compile + 全量单测 +
  `--test-stream-wav` 真链 + mock 全链。

- 版本控制：2026-09-02 已 git init 并首提交（main 分支）；config.toml、
  录音/输出/日志产物、.venv 均由 .gitignore 排除，提交前 `git status` 确认
  密钥文件未被追踪。

## GUI 线程模型（五线）

1. tkinter 主线程只碰界面，`after(200)` 轮询 电平/转写/进度 三个队列；
2. PortAudio 回调线程录音写盘 + 推电平 + `session.feed()`（只入队，绝不阻塞）；
3. 流式 reader 线程（会话内部）：收响应、收 definite 分句、断线重连；
4. 流式 sender 线程（会话内部）：从音频缓冲队列取帧发送；
5. 管线线程：散会后单开工作线程跑 流式收尾→全量映射→纪要→docx。
   转写回调只把 utterance 塞队列，增量报到映射在主线程做。

## PyInstaller 打包定版（2026-09-03）

- **冻结态 BASE_DIR 修复（关键，先于打包落地）**：`config_loader.py` / `pipeline.py` /
  `会议记录.py` / `streaming_asr.py` 原本都用 `os.path.dirname(os.path.abspath(__file__))`
  定 `BASE_DIR`，PyInstaller 单文件 exe 里 `__file__` 指向临时解压目录 `_MEIPASS`
  （退出即删），会导致 config.toml / 录音 / 输出 / 日志 全写到临时目录而丢失。
  修复 = `config_loader.py` 新增 `app_base_dir()`（`sys.frozen` 时用
  `os.path.dirname(sys.executable)`，否则用源码目录），其余三处改为 `from
  config_loader import app_base_dir; BASE_DIR = app_base_dir()`。改后 54 单测仍全绿。
- **打包命令**（在项目目录、用 venv python）：
  `./.venv/Scripts/python.exe -m PyInstaller --onefile --windowed --collect-all sounddevice --collect-all docx --name 会议记录 会议记录.py`
  - `--collect-all sounddevice` 兜 PortAudio DLL；`--collect-all docx` 兜 python-docx
    默认模板 `default.docx`（否则出稿时 `Document()` 会缺模板报错）。
  - 产物 `dist\会议记录.exe`，约 31M（31552768 字节）。
- **冒烟测试结论（2026-09-03，通过）**：用 `cmd //c start` 脱机（等价双击）拉起 exe
  约 16s，进程存活（`taskkill /F /IM 会议记录.exe` 命中 2 个 PID = bootloader 父 + 应用
  子进程，PyInstaller 单文件正常），无闪退；`日志/会议记录_20260903.log` 生成在 exe
  旁边（而非 _MEIPASS），证明 BASE_DIR 修复生效；windowed 模式下 `sys.stdout` 非 None、
  `StreamHandler(sys.stdout)` 不崩（实测，无需加 None 守卫）。
- **分发文件夹**：`D:\会议记录工具`，内含 `会议记录.exe` + 空白 `config.toml`（由
  `config.example.toml` 拷贝，两个 api_key 均空串，**绝不含真实 Key**）+ `使用说明书.md`
  + `使用说明书.docx`。
- **以后重打**：改代码后跑 `py_compile` + 全量单测，再执行上述打包命令，把新的
  `dist\会议记录.exe` 覆盖到 `D:\会议记录工具\会议记录.exe`；spec 文件 `会议记录.spec`
  保留在项目根目录，可复用；`build/`、`dist/` 打完后删掉。
- **说明书排版定版（2026-09-03，用户拍板）**：源 `D:\会议记录工具\使用说明书.md`
  （口语化、像朋友写给朋友，禁营销腔/粗体轰炸/⚠️/引用块），渲染脚本 `gen_manual_docx.py`
  （项目根目录，`./.venv/Scripts/python.exe gen_manual_docx.py` 生成 docx）。排版：A4、
  四边距 2.5cm；正文微软雅黑 11pt（rFonts 设 ascii/hAnsi/eastAsia/cs 全微软雅黑）、1.5 倍
  行距、段后 6pt；文档标题 20pt 加粗居中；章标题 14pt、小节 12pt 加粗**黑色**（不用内置
  Heading 样式，避免默认蓝绿）；列表用普通段落+缩进+手动编号；目录树代码块 Consolas 等宽
  10.5pt+浅灰底纹；粗体只保留按钮名【…】、关键警告、FAQ 问句，每段最多一处。改 md 后重跑
  脚本即可，勿手改 docx。
