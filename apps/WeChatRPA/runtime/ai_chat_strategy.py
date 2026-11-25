#!/usr/bin/env python3
"""
AI模拟聊天策略模块 - 增强版（内置策略）
支持基于图像的多用户聊天回复生成，集成视觉大模型
"""

import json
import base64
import os
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional


class ApiCallError(Exception):
    """API failure with enough metadata for the RPA layer to decide whether to stop."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
        user_message: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.user_message = user_message or message


# Zero-width / invisible characters some models emit at the start of a response.
# Python's str.strip() does NOT remove these, which is why they can cause
# json.loads to fail with 'Expecting value: line 1 column 1 (char 0)'.
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u200e\u200f\ufeff\u2028\u2029\u3000"


def _strip_invisible(text: str) -> str:
    """Trim ASCII whitespace and common zero-width / ideographic whitespace."""
    return text.strip().strip(_INVISIBLE_CHARS).strip()


def _repair_json_text(text: str) -> str:
    """Attempt to repair common LLM JSON output issues (unquoted keys, markdown fences)."""
    import re

    cleaned = _strip_invisible(text)

    # 1. Strip <think>...</think> reasoning blocks emitted by R1/Kimi-style models.
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = _strip_invisible(cleaned)

    # 2. Remove markdown code fences (possibly with a language tag).
    m = re.match(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", cleaned, re.DOTALL)
    if m:
        cleaned = _strip_invisible(m.group(1))

    # 3. Try to extract JSON object if there's text before/after.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]

    # 4. Quote bare property names (e.g.  messages: → "messages":)
    #    Avoid lookbehind (requires fixed-width in Python), use capture groups instead.
    def _quote_key(m: re.Match) -> str:
        """Wrap a bare JSON key in double quotes."""
        return f'{m.group(1)}"{m.group(2)}"{m.group(3)}'

    # Keys after { or ,
    cleaned = re.sub(
        r'([{,])\s*([a-zA-Z_]\w*)(\s*:)',
        _quote_key,
        cleaned,
    )
    # Keys at line starts (common in multi-line LLM JSON)
    cleaned = re.sub(
        r'^(\s*)([a-zA-Z_]\w*)(\s*:)',
        lambda m: f'{m.group(1)}"{m.group(2)}"{m.group(3)}',
        cleaned,
        flags=re.MULTILINE,
    )

    # 5. Remove trailing commas before } or ] (another common LLM mistake)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)

    return cleaned


def _preview_for_log(text: str, limit: int = 240) -> str:
    """Return a single-line preview of raw model output, safe for a log line."""
    if text is None:
        return "<None>"
    s = str(text)
    if not s:
        return "<empty>"
    # Show length + escaped preview so invisible chars are visible in logs.
    body = s if len(s) <= limit else s[:limit] + "…"
    return f"len={len(s)} repr={body!r}"


def _dump_parse_failure(text: str, tag: str) -> None:
    """Persist a failing LLM response so it can be inspected after the fact."""
    try:
        dump_dir = Path(__file__).resolve().parent / "logs" / "parse_failures"
        dump_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        (dump_dir / f"{ts}_{tag}.txt").write_text(text or "", encoding="utf-8")
    except Exception:
        # Never let diagnostic logging break the main flow.
        pass


def _robust_json_parse(text: str) -> Any:
    """Parse JSON with automatic repair for common LLM output issues.

    On failure, the raw content preview is emitted to stdout (which is Tee'd
    to the runtime log) and the payload is dumped to logs/parse_failures/ so
    the exact bytes can be inspected later. The original JSONDecodeError is
    then re-raised so callers can handle it as before.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    repaired = _repair_json_text(text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as e:
        print(f"[策略] JSON解析失败诊断: raw {_preview_for_log(text)}")
        if repaired != text:
            print(f"[策略] JSON解析失败诊断: repaired {_preview_for_log(repaired)}")
        _dump_parse_failure(text, "json_parse")
        raise e


def is_api_stop_error(error: BaseException) -> bool:
    """Whether an API error should stop monitoring instead of being treated as no-reply.

    只有「重试也没用」的错误才停程序：鉴权失败(401/403)、余额不足(402)、其它
    非可重试的 HTTP 状态。可重试类（DNS/超时/连接失败/429/5xx 且已重试满 3 次）
    绝不能停 —— 家宽 DNS 抖一下就把整个监控停掉，之后所有消息都不会再回复，
    表现正是「对面发了消息但没回复」（20261107_120821 日志：DNS 解析失败）。
    这类失败由 run_monitor 的「识别失败不落基准、下轮重试」分支处理。
    """
    if isinstance(error, ApiCallError):
        return not error.retryable
    return False


def api_error_user_message(error: BaseException) -> str:
    """User-facing text for API failures."""
    if isinstance(error, ApiCallError):
        return error.user_message
    return f"AI 调用异常，请稍后再试: {error}"


def _poll_queued_api_result(base_url: str, check_url: str, headers: dict, timeout_s: int = 24) -> Optional[Dict[str, Any]]:
    """202 排队后按 check_url 轮询结果。

    平台高峰期返回 202 {"queued":true,"check_url":"/api/queue/status/req_xxx"}，
    请求其实已经进队列，结果稍后才出来。这里最多等 timeout_s 秒，
    取到含 choices 的结果就原样返回；取不到返回 None（调用方当可重试处理）。
    """
    if not check_url:
        return None
    try:
        from urllib.parse import urljoin
        poll_url = urljoin(base_url, check_url)
    except Exception:
        poll_url = check_url
    deadline = time.time() + max(5, timeout_s)
    while time.time() < deadline:
        try:
            resp = requests.get(poll_url, headers={k: v for k, v in headers.items() if k.lower() != "content-type"}, timeout=(5, 10))
        except requests.exceptions.RequestException as e:
            print(f"[API] 排队轮询网络异常，稍后再试: {e}")
            time.sleep(2)
            continue
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                time.sleep(2)
                continue
            found = _find_choices_container(data)
            if found:
                return found
        time.sleep(2)
    return None


def _find_choices_container(data, depth: int = 0) -> Optional[Dict[str, Any]]:
    """在排队结果里找出带 choices 的那层（结构各接口不统一，这里宽容匹配）。"""
    if depth > 5 or not isinstance(data, (dict, list)):
        return None
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            return {"choices": choices}
        for v in data.values():
            found = _find_choices_container(v, depth + 1)
            if found:
                return found
        return None
    for v in data:
        found = _find_choices_container(v, depth + 1)
        if found:
            return found
    return None


def api_http_session() -> requests.Session:
    """
    调用 Moonshot 等公网 API 时使用的 Session。
    默认不信任 HTTP(S)_PROXY 等环境变量，避免本机配置了 127.0.0.1:7890 等代理但软件未启动时出现 ProxyError。
    若必须走系统代理才能上网，请设置环境变量 WECHAT_SKILL_USE_SYSTEM_PROXY=1。
    """
    s = requests.Session()
    if os.environ.get("WECHAT_SKILL_USE_SYSTEM_PROXY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return s
    s.trust_env = False
    return s


# ============================================================================
# 内置策略内容（根据任务要求提取）
# ============================================================================

BUILTIN_STRATEGY = """
这是用户设置的默认聊天策略。若无自定义策略，则执行此策略。

【核心要求】
- 对方问啥答啥，别绕弯子，别追加追问
- 回复 1~5 条：能一句说完不拆空话；对方问多项、发资料、需分点时才多条，多条用换行分隔，每条独立完整；别搞"理解+建议+追问"机械三段式
- 回得要像真人打字：短、碎、口语化
- 别暴露自己是AI，别问密码/钱/住址

【对方性别】以系统提示中的【对方性别】为准。
【聊天目标】以系统提示中的【本联系人聊天目标】为准（若有）。
"""

# 已移除：李梦渊联系人名专属策略（原 LIMENGYUAN_SISTER_STRATEGY）。需亲属语境请在 chat_strategy_config.json 中自写 strategy_text。

# ============================================================================
# 配置与工具函数
# ============================================================================

DEFAULT_STRATEGY_CONFIG_NAME = "chat_strategy_config.json"


def strategy_config_path_next_to_user_config(
    user_config_path: Optional[str],
) -> Optional[Path]:
    """与 user_config.json 同目录下的 chat_strategy_config.json 路径（文件未必存在）。"""
    if not user_config_path:
        return None
    return Path(user_config_path).resolve().parent / DEFAULT_STRATEGY_CONFIG_NAME


def load_chat_strategy_config(config_file: Optional[Path]) -> Dict[str, Any]:
    """读取 chat_strategy_config.json；缺文件或异常时返回空 dict。"""
    if not config_file or not config_file.is_file():
        return {}
    try:
        with open(config_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️ 读取聊天策略配置失败 ({config_file}): {e}")
        return {}


def _runtime_dir() -> Path:
    return Path(__file__).resolve().parent


def _current_agent_name() -> str:
    """从 my_contacts.json 读当前激活员工名；读不到返回空串。"""
    contacts_path = _runtime_dir() / "my_contacts.json"
    try:
        if contacts_path.is_file():
            data = json.loads(contacts_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("skill_name") or "").strip()
    except Exception:
        pass
    return ""


def load_skill_knowledge(agent_name: str = "") -> str:
    """读取当前员工技能包 references 下的客户知识（合并为文本）。

    技能目录：skills/<agent>/references/*.md。切换员工后，只注入当前员工的知识，
    避免串用到其他员工的客户资料。取不到返回空串。
    """
    agent = (agent_name or _current_agent_name()).strip()
    if not agent:
        return ""
    skill_dir = _runtime_dir() / "skills" / agent
    if not skill_dir.exists():
        # 目录名与 frontmatter name 不一致时兜底：按 SKILL.md 的 name 匹配
        root = _runtime_dir() / "skills"
        if root.exists():
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                md = next(d.rglob("SKILL.md"), None)
                if md is not None:
                    try:
                        txt = md.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    m = __import__("re").match(r'^---\s*\n(.*?)\n---', txt, __import__("re").DOTALL)
                    if m:
                        for line in m.group(1).split("\n"):
                            if line.strip().startswith("name:"):
                                if line.split(":", 1)[1].strip().strip('"').strip("'") == agent:
                                    skill_dir = d
                                    break
                if skill_dir.exists():
                    break
    if not skill_dir.exists():
        return ""

    parts: list[str] = []
    refs_dir = next((d for d in skill_dir.rglob("references") if d.is_dir()), None)
    if refs_dir is not None:
        for md in sorted(refs_dir.rglob("*.md")):
            try:
                content = md.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if content:
                parts.append(f"【{md.stem}】\n{content}")
    if not parts:
        return ""
    return "【客户知识（仅当前员工）】\n" + "\n\n".join(parts)


def reply_only_mode_enabled(user_config_path: Optional[str]) -> bool:
    """是否用「reply 有没有内容」来决定发不发（chat_strategy_config.json 的 reply_only_mode）。

    默认 true：模型写出内容就发，写成空就不发；模型输出的 should_reply 不再当发送闸门。
    改回 false 即回到「模型自己判 should_reply」的旧口径，出问题一个值就能回退。
    """
    data = load_chat_strategy_config(strategy_config_path_next_to_user_config(user_config_path))
    return bool(data.get("reply_only_mode", True))


def max_reply_messages_from_config(user_config_path: Optional[str]) -> int:
    """回复条数上限（chat_strategy_config.json 的 max_reply_messages），默认 5，范围 1~10。"""
    data = load_chat_strategy_config(strategy_config_path_next_to_user_config(user_config_path))
    try:
        n = int(data.get("max_reply_messages", 5))
    except (TypeError, ValueError):
        n = 5
    return max(1, min(10, n))


def reply_count_clause(max_n: int) -> str:
    """「回复条数」提示词片段；max_n=5 时与历史写死的 1~5 条文案语义一致。"""
    if max_n <= 1:
        return "每次回复只发 1 条消息。"
    mid = f"中间情况发 2~{max_n - 1} 条；" if max_n >= 4 else ""
    return (
        f"回复条数在 1~{max_n} 条之间随机变化，由内容重要性决定："
        f"不重要/简单回应发 1 条；重要、需详细介绍、客户在意的内容尽量发满 {max_n} 条"
        f"（如费用明细、机构实力、方案对比）；{mid}不要每次都发一样的条数。"
    )


def format_privacy_guardrails_block(user_config_path: Optional[str]) -> str:
    """
    根据与 user_config 同目录的 chat_strategy_config.json 生成【隐私与保密】段落。
    无配置文件时视为 privacy_strict_finance / privacy_strict_contacts 均为 True。
    配置文件存在且两项均为 False、且无自定义文案时返回空串（显式关闭）。
    """
    cfg_file = strategy_config_path_next_to_user_config(user_config_path)
    finance = True
    contacts = True
    custom = ""
    file_exists = bool(cfg_file and cfg_file.is_file())

    if file_exists:
        data = load_chat_strategy_config(cfg_file)
        finance = bool(data.get("privacy_strict_finance", True))
        contacts = bool(data.get("privacy_strict_contacts", True))
        cr = data.get("privacy_custom_rules")
        custom = str(cr).strip() if cr is not None else ""

    line_finance = (
        "- 不得索要、透露、推断或暗示任何密码、验证码、资金、银行账户、支付信息、保密标识等。"
    )
    line_contacts = (
        "- 不得在对话中发送或索取本人及他人（含朋友）的电话号码、微信号、住址等私密联系方式；"
        "不串联泄露不同联系人的信息。"
    )

    parts: list[str] = []
    if finance:
        parts.append(line_finance)
    if contacts:
        parts.append(line_contacts)
    if custom:
        parts.append("- 用户额外要求（须一并遵守）：")
        parts.append(custom)

    if file_exists and not finance and not contacts and not custom:
        return ""

    if not parts:
        return ""

    return "【隐私与保密（必须遵守）】\n" + "\n".join(parts)


def apply_ai_role_prefix(core: str, role: str) -> str:
    """非空 role 时在最前追加【角色设定】。"""
    r = (role or "").strip()
    if not r:
        return core
    return f"【角色设定】\n{r}\n\n{core}"


def resolve_strategy_text_for_contact(
    contact_id: Optional[str],
    user_config_path: Optional[str] = None,
) -> str:
    """
    选择策略正文并可选叠加 AI 角色前缀。

    正文优先级：chat_strategy_config.json 的 strategy_text（非空）> 内置 BUILTIN。
    若 JSON 中 ai_role 非空，在最终正文前追加【角色设定】。

    user_config_path 用于定位同目录的 chat_strategy_config.json；不传则不读 JSON 自定义层。
    """
    cfg_file = strategy_config_path_next_to_user_config(user_config_path)
    custom = load_chat_strategy_config(cfg_file) if cfg_file else {}
    ai_role = ""
    if custom:
        ar = custom.get("ai_role")
        if ar is not None:
            ai_role = str(ar).strip()

    st_json = ""
    if custom:
        raw = custom.get("strategy_text")
        if raw is not None:
            st_json = str(raw).strip()

    if st_json:
        core = st_json
    else:
        core = BUILTIN_STRATEGY

    # 叠加当前员工技能包里的客户知识，避免串用其他员工资料
    knowledge = load_skill_knowledge(ai_role)
    if knowledge:
        core = f"{core}\n\n{knowledge}"

    return apply_ai_role_prefix(core, ai_role)


def resolve_user_config_path(
    app_config_path: Optional[str] = None,
    merged_app_config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    VisualChatStrategy 需要「API 用户配置」JSON（如 user_config.json）。
    应用层传入的 app_config_path 往往是 YAML（如 wechat_automation_config.yaml），
    二者必须区分开。
    """
    if merged_app_config:
        wa = merged_app_config.get("wechat_assistant")
        if isinstance(wa, dict):
            explicit = wa.get("user_config_file")
            if explicit:
                return str(explicit)
        top = merged_app_config.get("user_config_file")
        if top:
            return str(top)
    if app_config_path:
        p = Path(app_config_path)
        if p.is_file() and p.suffix.lower() == ".json":
            return str(p)
        if p.is_file() and p.suffix.lower() in (".yaml", ".yml"):
            return "user_config.json"
    return "user_config.json"


def _session_api_key() -> str:
    """从启动器的登录会话里取 ApiKey，作为配置缺失时的兜底。

    工程里提交的 user_config.json 是模板（api_key 为空占位），真实密钥由壳子
    启动子程序时同步到运行目录那份。若直接从源码目录启动（或同步没生效），
    配置里的 api_key 就会是空串，请求会变成 `Authorization: Bearer ` 被后端
    判成 "Invalid API Key"。这里只读不写，避免把密钥落到被 git 跟踪的文件里。
    """
    try:
        p = Path(os.environ.get("APPDATA", "")) / "x.HR" / "session.json"
        if not p.exists():
            return ""
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("ApiKey") or data.get("api_key") or "").strip()
    except Exception:
        return ""


def load_user_config(config_path: str = "user_config.json") -> Dict[str, Dict[str, str]]:
    """加载用户API配置。

    兼容两种结构：
      1) 顶层账号字典：{ "default": { "api_key": ..., "model": ... } }
      2) 启动器下发的 lobster 段：{ "lobster": { "api_key": ..., ... } }

    文件缺失、为空或解析失败时返回空字典，由调用层在真正调用 API 时再报错，
    避免子程序在启动阶段因配置缺失直接崩溃。
    """
    if config_path is None:
        config_path = "user_config.json"

    account_config: Dict[str, Dict[str, str]] = {}
    path = Path(config_path)
    if not path.exists():
        print(f"  [配置] 用户配置文件不存在: {config_path}，使用空配置")
        return account_config

    try:
        raw_text = path.read_text(encoding="utf-8").strip()
        if not raw_text:
            print(f"  [配置] 用户配置文件为空: {config_path}，使用空配置")
            return account_config
        config = json.loads(raw_text)
    except Exception as e:
        print(f"  [配置] 解析用户配置失败: {e}，使用空配置")
        return account_config

    if not isinstance(config, dict):
        return account_config

    # 归一化：把 lobster 段当作一个默认账号条目。
    normalized: Dict[str, Any] = {}
    for user_id, cfg in config.items():
        if not isinstance(cfg, dict):
            continue
        normalized[str(user_id)] = cfg

    for user_id, cfg in normalized.items():
        if "api_key" not in cfg:
            continue
        # 配置里的密钥为空（模板占位 / 壳子没同步成功）时，回退到登录会话里的 ApiKey
        if not str(cfg.get("api_key") or "").strip():
            fallback = _session_api_key()
            if fallback:
                cfg["api_key"] = fallback
                print(f"  [配置] {user_id} 的 api_key 为空，已回退使用登录会话中的账号密钥")
            else:
                print(f"  [配置] {user_id} 的 api_key 为空，且未找到可用的登录会话密钥")
        if "model" not in cfg:
            cfg["model"] = "moonshot-v1-8k-vision-preview"
        if "base_url" not in cfg:
            cfg["base_url"] = "https://api.moonshot.cn/v1/chat/completions"
        if "gender" not in cfg:
            cfg["gender"] = "女"
        account_config[user_id] = cfg
    return account_config


def explicit_should_reply(result: Dict[str, Any], reply_only_mode: bool = False) -> bool:
    """是否发这条回复。

    reply_only_mode=true：模型写出非空 reply 就发（判定字段只作参考）。
    reply_only_mode=false：沿用旧口径，只有模型显式 should_reply=true 才发。
    """
    if reply_only_mode:
        return bool(str(result.get("reply") or "").strip())
    return result.get("should_reply") is True


def explicit_vision_should_reply(result: Dict[str, Any], reply_only_mode: bool = False) -> bool:
    """Vision replies need content (or explicit approval) and must not be our own last message."""
    if not explicit_should_reply(result, reply_only_mode):
        return False
    sender = str(result.get("last_message_sender") or "").strip().lower()
    return sender in ("other", "对方")


# 极短消息判定用：这些字符本身不承载语义
_BARE_PUNCT_CHARS = set(
    "。，、,.!！?？~～…;；:：\"'“”‘’()（）[]【】<>《》-—_+*/\\|= \t\r\n"
)
_SHORT_ACK_CORES = {"嗯", "哦", "呃", "啊", "呀", "哈", "呵", "好", "行", "是", "对", "ok"}


def is_bare_short_message(text: Any) -> bool:
    """判断最后一条消息是不是「纯标点 / 单字 / 单表情」这类极短接话信号。

    对方只发一个「。」「？」「嗯」或一个表情时，视觉模型倾向于判成
    「没有实际内容、无需回复」而静默跳过；实际这类消息是在等接话。
    调用方据此做一次定向重试，避免漏回。
    """
    s = str(text or "").strip()
    if not s:
        return False
    if s.startswith("[表情"):
        return True
    core = "".join(ch for ch in s if ch not in _BARE_PUNCT_CHARS).strip()
    if not core:
        # 只剩标点/空白 → 纯标点消息
        return True
    return len(core) <= 1 or core.lower() in _SHORT_ACK_CORES


def is_punctuation_only(text: Any) -> bool:
    """只含标点/空白的消息。我方回复不会只剩标点（发送前会剥掉句末标点），
    所以这类文本归属为 me 基本等于模型把对方气泡认错了。"""
    s = str(text or "").strip()
    if not s:
        return False
    return not "".join(ch for ch in s if ch not in _BARE_PUNCT_CHARS).strip()


def _format_recent_replies(recent_replies: Optional[list[str]]) -> str:
    items = [str(x).strip() for x in (recent_replies or []) if str(x).strip()]
    if not items:
        return ""
    lines = "\n".join(f"- {x[:160]}" for x in items[-5:])
    return (
        "\n\n【最近我方已发过的话（严禁重复同义表达）】\n"
        f"{lines}\n"
        "本轮不得再夸同一个点、不得复用「好酷/氛围感/画风/气质/风格」这类已用过的夸法；"
        "如果对方最新白色文字不是继续问照片，就优先接最新文字的情绪和话题。"
    )


def _format_chat_history_for_prompt(chat_history: Optional[list[dict]]) -> str:
    """Format normalized chat history for text-only reply generation."""
    lines: list[str] = []
    for msg in chat_history or []:
        if not isinstance(msg, dict):
            continue
        sender = str(msg.get("sender") or "?").strip()
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        ts = str(msg.get("timestamp") or msg.get("seen_at") or "").strip()
        label = "我方" if sender == "me" else "对方" if sender == "other" else sender
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"{prefix}{label}: {content}")
    return "\n".join(lines) if lines else "（暂无可用聊天记录）"


def get_user_gender(user_id: str = "default", config_path: str = "user_config.json") -> str:
    """获取用户性别"""
    try:
        config = load_user_config(config_path)
        if user_id in config:
            return config[user_id].get("gender", "女")
        else:
            return "女"
    except Exception as e:
        print(f"⚠️ 读取用户性别失败: {e}")
        return "女"


# ============================================================================
# 视觉聊天策略类
# ============================================================================

class VisualChatStrategy:
    """基于图像聊天的策略类，内置策略文本"""

    def __init__(self, user_config_path: str = "user_config.json"):
        self.user_config = load_user_config(user_config_path)
        self._strategy_user_config_path = user_config_path
        self._api_keys = {}
        self._models = {}
        self._base_urls = {}
        # Reuse a single HTTP session across calls to avoid repeated TCP handshakes
        self._session = api_http_session()

    def _get_base_url(self, user_id: str) -> str:
        if user_id not in self.user_config:
            raise ValueError(f"用户 {user_id} 未在配置文件中找到")
        if user_id not in self._base_urls:
            self._base_urls[user_id] = self.user_config[user_id].get("base_url", "https://api.moonshot.cn/v1/chat/completions")
        return self._base_urls[user_id]

    def _get_api_key(self, user_id: str) -> str:
        if user_id not in self.user_config:
            raise ValueError(f"用户 {user_id} 未在配置文件中找到")
        if user_id not in self._api_keys:
            self._api_keys[user_id] = self.user_config[user_id]["api_key"]
        return self._api_keys[user_id]

    def _get_user_model(self, user_id: str) -> str:
        return self.user_config[user_id].get("model", "moonshot-v1-8k")

    def _get_model_fallbacks(self, user_id: str) -> list:
        """获取fallback模型列表"""
        return self.user_config[user_id].get("model_fallbacks", [])

    def _default_api_user_id(self) -> str:
        if not self.user_config:
            raise ValueError("user_config 为空，无法调用 API")
        return next(iter(self.user_config))

    def _call_api(self, user_id: str, messages: list, response_format: dict = None, temperature: float = 0.7, max_tokens: int = 500) -> Dict[str, Any]:
        api_key = self._get_api_key(user_id)
        base_url = self._get_base_url(user_id)

        # 获取 moduleId（如果存在）
        module_id = self.user_config[user_id].get("moduleId")
        
        # 获取主要模型和fallback模型列表
        primary_model = self._get_user_model(user_id)
        fallback_models = self._get_model_fallbacks(user_id)
        all_models = [primary_model] + fallback_models


        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        # 调试：打印请求信息（仅打印关键信息，不打印完整的 messages）
        print(f"[API] 请求: base_url={base_url}")
        print(f"[API] 请求: payload keys=['model', 'messages', 'temperature', 'max_tokens', 'stream']")
        if module_id:
            print(f"[API] moduleId配置: {module_id}")

        max_attempts = 3
        retryable_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
        request_started = time.perf_counter()
        
        # 遍历所有模型（主模型+fallback）
        for model_idx, current_model in enumerate(all_models):
            print(f"[API] 尝试模型: {current_model} ({model_idx + 1}/{len(all_models)})")
            
            # 为当前模型创建payload
            # 获取 enable_thinking 配置（默认 False 关闭思考模式以减少延迟）
            enable_thinking = self.user_config[user_id].get("enable_thinking", False)

            payload = {
                "model": current_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                # Enable SSE streaming so the server sends tokens as they are generated.
                # This keeps the connection alive (avoids timeout on slow models) and
                # lets us log time-to-first-token for diagnostics.
                "stream": True,
                # Disable thinking/reasoning mode by default — reasoning models (Kimi K2,
                # DeepSeek-R1) spend a long time generating reasoning_content before any
                # actual content, causing perceived latency. Set enable_thinking=True in
                # user_config.json to re-enable if needed.
                "enable_thinking": enable_thinking,
            }

            # 如果配置了 response_format，添加到请求中
            if response_format:
                payload["response_format"] = response_format

            # 如果配置了 moduleId，添加到请求中
            if module_id:
                payload["moduleId"] = module_id  # 使用驼峰命名，与 user_config.json 保持一致
                print(f"[API] 已添加 moduleId: {module_id}")

            print(f"[API] 当前模型: {current_model}")
            print(f"[API] 思考模式: {'开启' if enable_thinking else '关闭（快速响应）'}")
            
            # 重试循环必须真的包住「请求 + 响应处理」整段。历史上这段
            # 缩进在 for attempt 之外，导致 except 里的 continue 绑到
            # for model_idx（不重试直接换模型）、break 直接跳出整个模型
            # 循环，函数走到末尾隐式 return None，上层 response["choices"]
            # 抛 'NoneType' object is not subscriptable —— 一次网络抖动就
            # 让这条消息永久不回复（20261107_120821 / 120900 日志实证）。
            for attempt in range(max_attempts):
                attempt_no = attempt + 1
                try:
                    # Reuse the persistent session (avoids TCP handshake per call).
                    # timeout=(connect_timeout, read_timeout_for_headers).
                    # With stream=True the body is read via iter_lines() which has
                    # no additional timeout, so slow generation won't trigger errors.
                    response = self._session.post(
                        base_url,
                        headers=headers,
                        json=payload,
                        timeout=(10, 120),
                        stream=True,
                    )
                except requests.exceptions.Timeout as e:
                    wait = 3 * attempt_no
                    print(f"[API] 网络异常，请稍后重试（第 {attempt_no}/{max_attempts} 次，超时）: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        continue

                    # 如果在当前模型上失败且还有其他模型可尝试，继续下一个模型
                    if model_idx < len(all_models) - 1:
                        print(f"[API] 当前模型 {current_model} 超时，尝试下一个模型...")
                        break  # 跳出当前模型的尝试循环，进入下一个模型

                    raise ApiCallError(
                        f"API 请求超时: {e}",
                        retryable=True,
                        user_message="网络异常，已自动重试 3 次仍连接不上，请稍后再试。",
                    ) from e
                except requests.exceptions.ConnectionError as e:
                    wait = 3 * attempt_no
                    print(f"[API] 网络异常，请稍后重试（第 {attempt_no}/{max_attempts} 次，连接失败）: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        continue

                    # 如果在当前模型上失败且还有其他模型可尝试，继续下一个模型
                    if model_idx < len(all_models) - 1:
                        print(f"[API] 当前模型 {current_model} 连接失败，尝试下一个模型...")
                        break  # 跳出当前模型的尝试循环，进入下一个模型

                    raise ApiCallError(
                        f"API 网络连接失败: {e}",
                        retryable=True,
                        user_message="网络异常，已自动重试 3 次仍连接不上，请检查网络或代理后稍后再试。",
                    ) from e
                except requests.exceptions.ChunkedEncodingError as e:
                    wait = 3 * attempt_no
                    print(f"[API] 网络异常，请稍后重试（第 {attempt_no}/{max_attempts} 次，传输中断）: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        continue

                    # 如果在当前模型上失败且还有其他模型可尝试，继续下一个模型
                    if model_idx < len(all_models) - 1:
                        print(f"[API] 当前模型 {current_model} 传输异常，尝试下一个模型...")
                        break  # 跳出当前模型的尝试循环，进入下一个模型

                    raise ApiCallError(
                        f"API 响应传输异常: {e}",
                        retryable=True,
                        user_message="网络异常，已自动重试 3 次仍失败，请稍后再试。",
                    ) from e
                except requests.exceptions.RequestException as e:
                    wait = 3 * attempt_no
                    print(f"[API] 网络异常，请稍后重试（第 {attempt_no}/{max_attempts} 次，请求异常）: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        continue

                    # 如果在当前模型上失败且还有其他模型可尝试，继续下一个模型
                    if model_idx < len(all_models) - 1:
                        print(f"[API] 当前模型 {current_model} 请求异常，尝试下一个模型...")
                        break  # 跳出当前模型的尝试循环，进入下一个模型

                    raise ApiCallError(
                        f"API 请求异常: {e}",
                        retryable=True,
                        user_message="网络异常，已自动重试 3 次仍失败，请稍后再试。",
                    ) from e

                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        # SSE streaming: accumulate content deltas from chunks
                        content_parts: list[str] = []
                        reasoning_seen = False
                        first_token_logged = False
                        first_chunk_keys = None
                        finish_reason = None
                        for line in response.iter_lines(decode_unicode=True):
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data = line[6:]
                                if data.strip() == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                    if not first_token_logged:
                                        first_token_logged = True
                                        first_chunk_keys = list(chunk.keys())
                                        ttft = time.perf_counter() - request_started
                                        print(f"[API] 首字节: {ttft:.1f}s")
                                    choices = chunk.get("choices")
                                    if not isinstance(choices, list) or not choices:
                                        continue
                                    delta = choices[0].get("delta", {})
                                    finish_reason = choices[0].get("finish_reason") or finish_reason
                                    c = delta.get("content")
                                    if c:
                                        content_parts.append(c)
                                    # Some reasoning models (Kimi K2, DeepSeek-R1)
                                    # stream tokens in `reasoning_content` and only
                                    # emit `content` at the end — track this so we
                                    # know why the stream looked empty.
                                    elif delta.get("reasoning_content"):
                                        reasoning_seen = True
                                except json.JSONDecodeError:
                                    continue
                        response.close()
                        full_content = "".join(content_parts)
                        elapsed = time.perf_counter() - request_started
                        # finish_reason=length 说明输出撞到 max_tokens 上限被硬切，
                        # JSON 里的 reply 字符串会被 _robust_json_parse「补引号」救回来，
                        # 结果就是发出去半句话（实测「你这边要是还没定下来，我」）。
                        if finish_reason == "length":
                            print("[API] 警告: 输出被 max_tokens 截断（finish_reason=length），回复可能缺半句")

                        if full_content:
                            print(f"[API] 成功: {elapsed:.1f}s")
                            return {
                                "choices": [
                                    {
                                        "message": {
                                            "content": full_content,
                                            "role": "assistant",
                                        }
                                    }
                                ]
                            }

                        # SSE 流式响应内容为空→某些模型不支持 stream+response_format 的组合
                        # 自动回退为非流式请求
                        reason_hint = " (仅见 reasoning_content，未见 content)" if reasoning_seen else ""
                        print(f"[API] SSE内容为空（chunk_keys={first_chunk_keys}{reason_hint}），回退到非流式请求...")

                        def _try_non_stream(content_only: bool = False):
                            """尝试非流式请求，当 content_only=True 时跳过 response_format。"""
                            p = {k: v for k, v in payload.items() if k != "stream"}
                            if content_only:
                                p.pop("response_format", None)
                            resp = self._session.post(base_url, headers=headers, json=p, timeout=(10, 120))
                            if resp.status_code != 200:
                                return None
                            result = resp.json()
                            raw = (result.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                            if not raw.strip():
                                return None
                            return result

                        # 第1次回退：去掉 stream（保留 response_format）
                        result = None
                        for attempt_desc, content_only in [("保留 response_format", False), ("去掉 response_format", True)]:
                            result = _try_non_stream(content_only=content_only)
                            if result:
                                ns_elapsed = time.perf_counter() - request_started
                                print(f"[API] 非流式回退成功（{attempt_desc}）: {ns_elapsed:.1f}s")
                                return result
                            else:
                                print(f"[API] 非流式回退（{attempt_desc}）无内容，尝试其他方式...")

                        # 所有回退都失败，返回空内容让上层处理
                        print(f"[API] 成功（空内容）: {elapsed:.1f}s")
                        return {
                            "choices": [
                                {
                                    "message": {
                                        "content": full_content,
                                        "role": "assistant",
                                    }
                                }
                            ]
                        }
                    else:
                        # Non-streaming fallback (backend ignored stream=True)
                        result = response.json()
                        elapsed = time.perf_counter() - request_started
                        print(f"[API] 成功: {elapsed:.1f}s")
                        return result

                if response.status_code == 202:
                    # 平台高峰期会把请求压进队列并返回 202
                    # {"queued":true,"check_url":"/api/queue/status/req_..."}。
                    # 以前这里落到最下面的通用 raise（retryable=False），
                    # is_api_stop_error 判为致命 → 打印[停止] → 整轮监控退出。
                    # 现在：先按 check_url 轮询结果，拿不到再当作可重试处理。
                    body = response.text or ""
                    check_url = ""
                    try:
                        check_url = str((json.loads(body) or {}).get("check_url") or "")
                    except Exception:
                        check_url = ""
                    result = _poll_queued_api_result(base_url, check_url, headers)
                    if result:
                        elapsed = time.perf_counter() - request_started
                        print(f"[API] 排队结果已取回: {elapsed:.1f}s")
                        return result
                    wait = 3 * attempt_no
                    print(f"[API] 请求被排队（HTTP 202），第 {attempt_no}/{max_attempts} 次未取回结果，{wait}s 后重试")
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        continue
                    if model_idx < len(all_models) - 1:
                        print(f"[API] 当前模型 {current_model} 排队未完成，尝试下一个模型...")
                        break
                    raise ApiCallError(
                        f"API 调用失败: 202 - {body}",
                        status_code=202,
                        retryable=True,
                        user_message="AI 服务排队繁忙，稍后会自动重试。",
                    )

                body = response.text or ""
                if response.status_code == 402 or "insufficient_quota" in body.lower() or "insufficient balance" in body.lower():
                    raise ApiCallError(
                        f"API 调用失败: {response.status_code} - {body}",
                        status_code=response.status_code,
                        retryable=False,
                        user_message="AI 服务余额不足，请充值或更换可用模型配置后再试。",
                    )
                if response.status_code in (401, 403):
                    raise ApiCallError(
                        f"API 调用失败: {response.status_code} - {body}",
                        status_code=response.status_code,
                        retryable=False,
                        user_message="AI 服务鉴权失败，请检查 API Key、base_url、model 或 moduleId 配置。",
                    )
                if response.status_code in retryable_statuses:
                    wait = 3 * attempt_no
                    retry_after = response.headers.get('Retry-After')
                    if retry_after:
                        try:
                            wait = int(retry_after)
                            print(f"[API] 网络/API 繁忙，请稍后重试（第 {attempt_no}/{max_attempts} 次，HTTP {response.status_code}，服务器要求 {wait} 秒）...")
                        except ValueError:
                            print(f"[API] 网络/API 繁忙，请稍后重试（第 {attempt_no}/{max_attempts} 次，HTTP {response.status_code}）...")
                    else:
                        print(f"[API] 网络/API 繁忙，请稍后重试（第 {attempt_no}/{max_attempts} 次，HTTP {response.status_code}）...")
                    if attempt < max_attempts - 1:
                        time.sleep(wait)
                        continue

                    # 如果在当前模型上失败且还有其他模型可尝试，继续下一个模型
                    if model_idx < len(all_models) - 1:
                        print(f"[API] 当前模型 {current_model} 失败，尝试下一个模型...")
                        break  # 跳出当前模型的尝试循环，进入下一个模型

                    raise ApiCallError(
                        f"API 调用失败: {response.status_code} - {body}",
                        status_code=response.status_code,
                        retryable=True,
                        user_message="网络或 AI 服务繁忙，已自动重试 3 次仍失败，请稍后再试。",
                    )
                raise ApiCallError(
                    f"API 调用失败: {response.status_code} - {body}",
                    status_code=response.status_code,
                    retryable=False,
                    user_message=f"AI 服务调用失败（HTTP {response.status_code}），请检查模型服务配置后再试。",
                )

        # 理论上不可达：每个模型的最后一轮失败都会 raise，成功则 return。
        # 保留兜底，绝不让函数隐式返回 None —— 上层 `response["choices"]`
        # 一旦拿到 None 会抛 'NoneType' object is not subscriptable，
        # 把这条真实消息直接吞掉（20261107_120821 / 120900 日志实证）。
        raise ApiCallError(
            "API 调用未返回结果（所有模型均已尝试）",
            retryable=True,
            user_message="AI 服务无响应，请稍后重试。",
        )

    def generate_reply(
        self,
        contact_id: str,
        chat_history: list,
        gender: str = "未知",
        chat_goal: Optional[str] = None,
    ) -> tuple:
        """
        根据结构化聊天历史生成回复（纯文本，供 main / 混合助手使用）。
        API 账号取 user_config 中的第一个条目；contact_id 为微信侧昵称。
    
        Args:
            chat_goal: 非空时与视觉路径一致，输出含 goal_reached / handoff_message；达成时返回 ("", "goal_reached_handoff")。
    
        Returns:
            (reply_text, status)，status 为 "success"、"goal_reached_handoff" 或错误说明字符串。
        """
        user_id = self._default_api_user_id()
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        strategy_text = resolve_strategy_text_for_contact(
            contact_id,
            user_config_path=self._strategy_user_config_path,
        )
        print(f"[策略] 已加载聊天策略: {len(strategy_text)} 字；目标={chat_goal or '无'}")
        system_prompt = self._build_system_prompt(
            strategy_text,
            gender,
            current_time_str,
            chat_goal=chat_goal,
            channel="text",
            contact_display_name=contact_id,
        )

        lines = []
        for msg in chat_history or []:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender", "?")
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")
            lines.append(f"[{ts}] {sender}: {content}")
        transcript = "\n".join(lines) if lines else "（暂无最近消息，可适当主动破冰）"

        user_message = {
            "role": "user",
            "content": f"以下为与「{contact_id}」的最近聊天：\n{transcript}\n\n请只输出 JSON。",
        }

        cg = (chat_goal or "").strip()
        try:
            response = self._call_api(
                user_id=user_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    user_message,
                ],
                response_format={"type": "json_object"},
                temperature=0.55,
                max_tokens=300,
            )
            raw = response["choices"][0]["message"]["content"]
            result = _robust_json_parse(raw)
            if cg and bool(result.get("goal_reached", False)):
                hm = (result.get("handoff_message") or "").strip()
                if hm:
                    print(f"[策略] 聊天目标已达成，转人工提示: {hm[:200]}")
                return "", "goal_reached_handoff"
            if not explicit_should_reply(result):
                return "", "skipped_should_not_reply"
            reply = (result.get("reply") or "").strip()
            if not reply:
                return "", "empty_reply"
            return reply, "success"
        except json.JSONDecodeError as e:
            err = str(e)
            print(f"[策略] generate_reply JSON解析失败: {err}")
            return "", err
        except Exception as e:
            err = str(e)
            print(f"[策略] generate_reply 失败: {err}")
            if is_api_stop_error(e):
                return "", f"api_should_stop:{api_error_user_message(e)}"
            return "", err

    def generate_chat_progress_summary(self, contact_id: str, chat_history: list) -> str:
        """简要进度描述，供 main 报告使用（不调用 API）。"""
        msgs = chat_history or []
        n = len(msgs)
        if n == 0:
            return f"{contact_id}: 暂无聊天记录"
        last = msgs[-1]
        preview = (last.get("content") or "")[:48]
        return f"{contact_id}: 共{n}条，末条摘要「{preview}」"

    def generate_reply_from_text_history(
        self,
        contact_id: str,
        chat_history: list[dict],
        gender: str = "未知",
        chat_goal: Optional[str] = None,
        recent_replies: Optional[list[str]] = None,
        allow_proactive: bool = False,
    ) -> Dict[str, Any]:
        """
        根据已提取并落盘的文字聊天记录生成回复。
        这个路径不再把截图直接交给回复模型，图片/表情应已在提取阶段变成文字描述。
        """
        user_id = self._default_api_user_id()
        max_reply = max_reply_messages_from_config(self._strategy_user_config_path)
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        strategy_text = resolve_strategy_text_for_contact(
            contact_id,
            user_config_path=self._strategy_user_config_path,
        )
        print(f"[策略] 已加载聊天策略: {len(strategy_text)} 字；目标={chat_goal or '无'}")
        system_prompt = self._build_system_prompt(
            strategy_text,
            gender,
            current_time_str,
            chat_goal=chat_goal,
            channel="text",
            contact_display_name=contact_id,
        )

        transcript = _format_chat_history_for_prompt(chat_history)
        recent_note = _format_recent_replies(recent_replies)
        proactive_note = ""
        if allow_proactive:
            proactive_note = (
                "\n\n【主动发送许可】"
                "\n- 当前已允许主动发一条消息，但必须承接历史上下文。"
                "\n- 不得复述上一条我方消息，不得重复追问已确认的信息。"
            )

        user_message = {
            "role": "user",
            "content": (
                f"以下是与「{contact_id}」按联系人分类保存的最近聊天记录，图片和表情已替换成文字描述：\n"
                f"{transcript}\n\n"
                "请只根据这份文字记录判断是否回复并生成回复。\n"
                "硬规则：\n"
                "- 如果最后一条记录是我方，should_reply=false，reply为空，除非上方明确给了主动发送许可。\n"
                "- 如果最后一条记录是对方，且是提问、回应、确认、表达情绪或明显在等我方接话，should_reply=true。\n"
                "- 回复只能是将要发给对方的话，不要写分析、理由、策略、括号注释。\n"
                "- " + reply_count_clause(max_reply) + "多条之间用换行分隔（每条另起一行），每条是独立完整的一句话；不要长篇安慰或总结。\n"
                "- 默认不要用「哈哈」「嘻嘻」「嘿嘿」或 emoji 开头，除非对方最后一句明显是在开玩笑。\n"
                "- 不能复读或改写对方最后一句，不能重复最近我方已经表达过的意思。\n"
                "- 对方已经确认过的信息，不能换个问法再问一遍。\n"
                + proactive_note
                + recent_note
                + "\n\n输出严格 JSON。"
            ),
        }

        try:
            response = self._call_api(
                user_id=user_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    user_message,
                ],
                response_format={"type": "json_object"},
                temperature=0.55,
                max_tokens=300,
            )
            raw = response["choices"][0]["message"]["content"]
            result = _robust_json_parse(raw)
            last_msg = next(
                (
                    msg
                    for msg in reversed(chat_history or [])
                    if isinstance(msg, dict) and str(msg.get("content") or "").strip()
                ),
                {},
            )
            sender = result.get("last_message_sender") or last_msg.get("sender") or "unknown"
            text = result.get("last_message_text") or last_msg.get("content") or ""
            return {
                "should_reply": explicit_should_reply(result),
                "reply": result.get("reply", ""),
                "last_message_sender": sender,
                "last_message_side": result.get("last_message_side", "unknown"),
                "last_message_text": text,
                "goal_reached": bool(result.get("goal_reached", False)),
                "handoff_message": (result.get("handoff_message") or "").strip(),
            }
        except json.JSONDecodeError as e:
            err = str(e)
            print(f"[策略] 文字历史JSON解析失败: {err}")
            return {
                "should_reply": False,
                "reply": "",
                "last_message_sender": "unknown",
                "last_message_side": "unknown",
                "last_message_text": "",
                "goal_reached": False,
                "handoff_message": "",
                "error": err,
                "api_should_stop": False,
                "api_user_message": "",
            }
        except Exception as e:
            err = str(e)
            print(f"[策略] 文字历史生成回复失败（不会发送到微信）: {err}")
            return {
                "should_reply": False,
                "reply": "",
                "last_message_sender": "unknown",
                "last_message_side": "unknown",
                "last_message_text": "",
                "goal_reached": False,
                "handoff_message": "",
                "error": err,
                "api_should_stop": is_api_stop_error(e),
                "api_user_message": api_error_user_message(e) if is_api_stop_error(e) else "",
            }

    def generate_reply_from_image(
        self,
        user_id: str,
        image_url: str,
        gender: str = "未知",
        current_time: Optional[datetime] = None,
        contact_display_name: Optional[str] = None,
        long_cycle_monitor: bool = False,
        chat_goal: Optional[str] = None,
        recent_replies: Optional[list[str]] = None,
        local_last_sender: Optional[str] = None,
        allow_proactive: bool = False,
        local_last_band: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        根据聊天截图生成回复

        Args:
            user_id: 用户标识
            image_url: 聊天截图的公网URL（支持data:image/...格式）
            gender: 对方性别
            current_time: 当前时间
            contact_display_name: 当前微信备注/昵称（写入系统提示，便于对齐身份与目标）
            chat_goal: 可选，非空时模型需判断是否达成该目标；达成则 goal_reached=true 并建议转人工

        Returns:
            {"should_reply": bool, "reply": str, "goal_reached"?: bool, "handoff_message"?: str, 可选 "error": str}
        """
        strategy_text = resolve_strategy_text_for_contact(
            contact_display_name,
            self._strategy_user_config_path,
        )
        print(f"[策略] 已加载聊天策略: {len(strategy_text)} 字；目标={chat_goal or '无'}")
        max_reply = max_reply_messages_from_config(self._strategy_user_config_path)
        reply_only_mode = reply_only_mode_enabled(self._strategy_user_config_path)

        if current_time is None:
            current_time = datetime.now()
        current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")

        system_prompt = self._build_system_prompt(
            strategy_text,
            gender,
            current_time_str,
            chat_goal=chat_goal,
            channel="vision",
            contact_display_name=contact_display_name,
        )

        monitor_note = ""
        if long_cycle_monitor:
            monitor_note = (
                "\n\n【补充规则】"
                "\n- 截图界面变化≠对方回复（时间更新、光标闪烁等都会让截图变化），只有出现你没见过的左侧对方气泡才算新消息。"
                "\n- 严禁连续多轮回复同一话题：即使对方回了消息，本轮回复也必须与上轮你发的有区别，不能车轱辘话来回说。"
                "\n- 严禁仅因「截图有变化」或「需要推进目标」就在对方没说话时主动发言。"
                "\n- 表情包、图片、视频号、链接、文件等非文字内容，只有左侧对齐、头像在左边时才属「对方发来的新消息」。"
                "\n- 任何右侧对齐、头像在右边的文字、图片缩略图、表情、卡片、文件，都是我方已发送内容，reply 留空。"
                "\n- 对方只发一个标点（「。」「？」「，」「~」等）、单个字（「嗯」「哦」「啊」「好」「是」等）或一个表情，"
                "也是一条真实的对方消息，不是噪声、不是界面残留：必须记为 last_message_sender=other，"
                "并在 reply 里给一句自然的口语回应；禁止复读该标点本身。"
                "这种极短消息通常表示对方在等你接话或回应刚才的话题，判成「无需回复」会导致对话直接断掉。"
                "\n- 若最后一条白色消息是文字，必须优先回应这句文字本身；不要继续围绕更早的图片发挥。"
                "\n- " + reply_count_clause(max_reply) + "多条之间用换行分隔（每条另起一行），每条是独立完整的一句话；不要连续夸图、不要重复刚才已经表达过的意思。"
                "\n- 如果最新白色文字是在评价「你说话/聊天/有意思/开心/哈哈/嘻嘻」等互动感受，只接这句话，禁止再提照片、画风、风格、氛围感、气质。"
                "\n- 如果上一轮已经评价过照片，本轮除非对方明确继续问照片，否则不要再评价照片。"
                "\n- 不要每次都用「哈哈」「嘻嘻」「嘿嘿」或 emoji 开头；最近 3 轮用过笑声开头时，本轮必须换一种开头或直接回答。"
                "\n- reply 字段只写会直接发给对方的消息，" + reply_count_clause(max_reply) + "多条之间用换行分隔（每条另起一行），不要用序号、分号或标点堆砌；每条是独立完整的一句话。禁止写任何分析、解释、策略、理由，例如「这回复看起来」「可以顺势」「比如问」「说明对方」「建议」。"
                "\n- 严禁复读或改写对方最后一句原话；如果对方说「会啊，我还挺喜欢去的嘞」，不能回复同一句，应该顺着话题接下去。"
                "\n- 如果最新白色消息是「会啊」「喜欢」「去过」「经常」这类对上一句问题的肯定回答，必须承认这个已知事实，禁止再次问同一个是/否问题。"
                "\n- 例：我方刚问「会去漫展吗」，对方答「会啊」，本轮不能问「你是经常去漫展玩吗」；应问更具体的新问题，如最近哪次印象深、喜欢哪类展、一般和谁去。"
                "\n- 聊天策略是最高优先级：必须遵守策略里的语气、人设、阶段动作和安全边界，不能脱离策略裸聊。"
                "\n- 聊天目标是中等优先级：当话题自然适合时推进关系/邀约/确认时间地点；如果当前句子更适合正常接话，可以先接住话题，不要每轮都硬拽到邀约。"
                "\n- 不要连续围绕同一个比喻/玩笑/表演感话题空转；可以接梗半句，但要保持真人感。"
                "\n- 如果当前目标是线下约会/见面/吃饭，且最新白色消息是「收到」「ok」「好的」「借你吉言」「借你的吉言」「接你吉言」「接你的吉言」「那必须」等接话，不要判定为无需回复，也不要只回复「好嘞」；可以顺势给一个低压力邀约。"
                "\n- 低压力邀约示例：等你忙完这阵子，找天一起吃个饭/喝杯奶茶/出去逛逛。不要写得像通知或推销。"
            )
        cg = (chat_goal or "").strip()
        goal_note = ""
        if cg:
            goal_note = "\n\n达成判定、未达成时的回复结构与 JSON 字段以系统提示为准；请严格按系统提示输出 JSON。"
        proactive_override_note = ""
        if allow_proactive:
            proactive_override_note = (
                "\n\n【主动发送模式（最高优先）】"
                "\n- 当前已进入主动发送模式：即使聊天窗口为空或最后一条消息是我方发出的，也必须写出要发送的内容（reply 不能留空）。"
                "\n- 如果聊天窗口完全为空（没有任何历史消息），生成一句自然的招呼语作为破冰，例如\"哈喽，最近怎么样？\"或匹配策略的轻松话题开场。"
                "\n- 如果聊天窗口有历史但最后一条是我方的消息，承接上一条我方消息的情绪和话题，自然延伸，不要复制粘贴原话。"
                "\n- 上述两条主动消息的规则优先级高于 monitor_note 中所有关于\"对方没说话就不发\"的约束。"
            )
        recent_note = _format_recent_replies(recent_replies)
        local_sender_note = ""
        lsender = (local_last_sender or "").strip().lower()
        if lsender in ("me", "other"):
            local_sender_note = (
                "\n\n【本地像素辅助判断】"
                f"\n- 程序根据气泡位置/颜色预判：最后一条可见消息发送者是 {lsender}。"
                "\n- 如果截图里最后气泡方向不清楚，优先参考这个预判；但如果你能明确看出相反方向，请按截图事实输出。"
            )
            if lsender == "me":
                local_sender_note += (
                    "\n- 特别注意：若最底部是「聊天记录/合并转发」卡片（带『聊天记录』标签、或含多条消息缩略的对话卡片），它是对方发来的，应视为对方消息并正常回复，即使预判为 me 也要回复。"
                )
        # 主动发送模式与「末条是我方就不回」直接冲突，必须在这里把那条规则换掉，
        # 否则模型会遵守后者、给出空 reply，主动阶梯就会一直空转。
        if allow_proactive:
            tail_rule = (
                "本次是主动发送模式：即使最后一条可见消息是我方发出的或窗口为空，"
                "也必须写出要发送的新内容，reply 不能为空字符串，也不能重复上一条已发内容。"
            )
            closing_line = "请根据图片中的聊天记录，按策略要求写一条现在要主动发出去的消息。输出JSON格式。"
        else:
            tail_rule = "如果最后一条可见消息是我方，reply 必须为空字符串（程序按 reply 是否有内容决定发不发）。"
            closing_line = "请根据图片中的聊天记录，按照策略要求，判断是否应该回复，并生成回复内容。输出JSON格式。"
        instruction_core = (
            "\u003C!--- 先看气泡颜色，其他规则在下面 --->"
            "先判断截图中最靠下的可见消息是谁发的："
            "左侧对齐、头像在左边=对方；右侧对齐、头像在右边=我方。"
            "绿色气泡=我方；右侧图片缩略图/表情/卡片/文件即使不是绿色气泡，也是我方。"
            "左侧白色气泡下方若挂着一个浅灰色小卡片（引用回复框），它是对方消息的一部分：整条消息的发送者仍是对方，灰色卡片里引用的我方文字不代表最后一条是我方发的。"
            "微信的「合并转发/聊天记录」卡片（顶部带「XX和XX的聊天记录」标题、或含「聊天记录」标签，内容是多条消息缩略的对话）是对方转发过来的，属于对方发来的新消息：即使卡片里缩略了我方绿色气泡或右侧内容，它仍是对方这条消息的一部分，必须视为对方最后一条消息并正常回复，不能因为卡片里有我方内容就判定为「我方最后一条」。若程序预判为 me，也请以这个事实为准进行回复。"
            + tail_rule
            + "如果整张截图只有顶部一两条消息、下方是大片空白（短对话），顶部那条就是对方最后一条可见消息，必须认真回应它，不能当成无关内容。"
            "忽略输入框、工具栏以及其中任何未发送的草稿文字，只分析聊天气泡区域里已经发出的消息；"
            "位于输入框里的文字、代码、命令等一律不算聊天记录。"
            "消息区底部若出现灰色的「对方正在输入…」「正在输入」一类提示，它只是输入状态提示，不是聊天消息、不算最后一条消息，也不能据此判定需要回复：要看它上方真正的气泡。"
            "注意：必须严格按气泡的垂直位置判断——最底部的那条才是最后一条消息，顶部标题/名字不是消息。\n\n"
            "【长度硬约束】reply 里最多写 3 条消息（用换行分隔），每条不超过 20 个字，"
            "不要写长段落、不要写括号补充说明、不要把一句话拆成两三行；"
            "宁可少说一句，也不要写到一半被截断。\n\n"
            + closing_line
        )
        user_message = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {
                    "type": "text",
                    "text": (
                        instruction_core
                        + monitor_note
                        + proactive_override_note
                        + local_sender_note
                        + goal_note
                        + recent_note
                    ),
                },
            ],
        }

        try:
            response = self._call_api(
                user_id=user_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    user_message,
                ],
                response_format={"type": "json_object"},
                temperature=0.8,
                max_tokens=1200,
            )

            raw = response["choices"][0]["message"]["content"]
            try:
                result = _robust_json_parse(raw)
            except json.JSONDecodeError as first_err:
                # 一些模型在首次非流式回退后会输出非 JSON 文本（例如中文解释、
                # <think> 块、或先说一句“好的”再输出JSON）。重试一次，要求严格只输出 JSON。
                print(f"[策略] 视觉回复JSON解析失败，重试一次: {first_err}")
                print(f"[策略] 视觉回复原始内容: {_preview_for_log(raw)}")
                _dump_parse_failure(raw or "", "vision_first")
                strict_user_message = {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {
                            "type": "text",
                            "text": (
                                "上一次你的输出不是合法 JSON，无法解析。请重新输出，只允许返回一个 JSON 对象，"
                                "不要任何前后缀、不要 markdown 代码块、不要 <think> 或解释文字。"
                                "\n\n字段要求："
                                "\n- should_reply: boolean"
                                "\n- reply: string"
                                "\n- last_message_sender: \"me\" | \"other\" | \"unknown\""
                                "\n- last_message_side: \"left\" | \"right\" | \"unknown\""
                                "\n- last_message_text: string"
                                + ("\n- goal_reached: boolean\n- handoff_message: string" if cg else "")
                                + "\n\n现在请直接输出 JSON，以 { 开头、以 } 结尾。"
                            ),
                        },
                    ],
                }
                response = self._call_api(
                    user_id=user_id,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        user_message,
                        {
                            "role": "assistant",
                            "content": raw if isinstance(raw, str) else "",
                        },
                        strict_user_message,
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=500,
                )
                raw = response["choices"][0]["message"]["content"]
                result = _robust_json_parse(raw)

            if allow_proactive:
                # 主动模式：空窗口/末条为我方也允许发送，直接采用模型的 should_reply 布尔值，
                # 不套用「末条必须是对方」的被动闸门（否则招呼语会被降级拦截）。
                vision_should = explicit_should_reply(result, reply_only_mode)
            else:
                vision_should = explicit_vision_should_reply(result, reply_only_mode)
            # reply_only_mode 下，模型没写内容才算「不回」；它写的 should_reply 只当参考，
            # 否则会出现「写了话却判不回」的自相矛盾（实测真实截图出现过）。
            reply_text = str(result.get("reply") or "").strip()
            # debug 088a05 实证：temp=0.8 下视觉判定存在 ~1/5 翻转率（同一截图
            # 会把我方长消息误读为末条 → should_reply=false → 漏回）。本地像素
            # 判定修复后已验证可靠（离线 5/5）；原样低温重试仍有 1/5 失败，
            # 注入气泡坐标的接地重试实测 5/5 通过。
            vision_last_other = (
                str(result.get("last_message_sender") or "").strip().lower() == "other"
            )
            local_other = (local_last_sender or "").strip().lower() == "other"
            # 对方只发一个句号/单字时，模型容易判成「没有实际内容 → 无需回复」。
            # 只要视觉或本地像素任一认定末条是对方，且内容是纯标点/极短，就走定向重试兜底。
            bare_short = is_bare_short_message(result.get("last_message_text"))
            # 模型把「只有一个句点」的气泡当成界面残留、记成 me 的情况：
            # 我方回复发送前会剥掉句末标点，不会出现纯标点消息，所以这里可控地判为对方。
            misattributed_bare = (
                is_punctuation_only(result.get("last_message_text"))
                and not vision_last_other and not local_other
            )
            need_retry = (
                not allow_proactive
                and vision_should is False
                and (local_other or (vision_last_other and bare_short) or misattributed_bare)
            )
            if need_retry:
                band = local_last_band or {}
                band_top = band.get("top")
                band_bottom = band.get("bottom")
                band_x = band.get("median_x")
                bare_note = ""
                if vision_last_other and bare_short:
                    bare_note = (
                        "\n- 最后一条是对方发来的极短消息（只有标点/单个字/表情），"
                        "它不是在结束对话，而是在等你接话：必须 should_reply=true，"
                        "生成一句自然的口语回应，不要复读这个标点，也不要回一个标点。"
                    )
                elif misattributed_bare:
                    bare_note = (
                        "\n- 截图最底部那条只有一个标点符号的气泡是对方（左侧白色气泡）发来的，"
                        "不是界面残留、也不是我方消息：last_message_sender 必须记为 other，"
                        "并给一句自然的口语回应；我方从不发只含标点的消息。"
                    )
                if band_top is not None and band_bottom is not None:
                    grounded_text = (
                        f"【像素检测纠正（最高优先）】程序扫描确认：截图最底部 y={band_top}-{band_bottom} "
                        f"处存在一条左侧白色文字气泡（对方消息，中心 x≈{band_x}），"
                        "它上方才是我方绿色长气泡。上一条回复把绿色长气泡误判成了最后一条消息。"
                        "请重新只看截图最底部区域，把那条左侧白色气泡作为 last_message，输出 JSON。"
                        + bare_note
                    )
                    print("[策略] 本地=other 但视觉判定未通过，注入气泡坐标 temperature=0.1 重试一次")
                elif bare_note:
                    grounded_text = "【纠正（最高优先）】" + bare_note + "\n请按系统提示输出 JSON：should_reply=true，reply 为一句自然的口语回应。"
                    print("[策略] 对方发来极短消息但判定无需回复，定向重试一次")
                else:
                    grounded_text = None
                    print("[策略] 本地=other 但视觉判定未通过，temperature=0.1 重试一次")
                try:
                    retry_messages = [{"role": "system", "content": system_prompt}]
                    if grounded_text:
                        retry_messages.append({
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {"type": "text", "text": grounded_text},
                            ],
                        })
                    else:
                        retry_messages.append(user_message)
                    retry_response = self._call_api(
                        user_id=user_id,
                        messages=retry_messages,
                        response_format={"type": "json_object"},
                        temperature=0.1,
                        max_tokens=700,
                    )
                    retry_result = _robust_json_parse(retry_response["choices"][0]["message"]["content"])
                    if explicit_vision_should_reply(retry_result, reply_only_mode):
                        result = retry_result
                        vision_should = True
                        print("[策略] 重试后判定通过，采用重试结果")
                    elif (
                        (
                            str(retry_result.get("last_message_sender") or "").strip().lower() == "other"
                            and is_bare_short_message(retry_result.get("last_message_text"))
                        )
                        or is_punctuation_only(retry_result.get("last_message_text"))
                    ):
                        # 末条确认是对方的极短消息（句号/单字），或只有标点（我方不会发纯标点）：
                        # 模型仍坚持不回属于判定失误，直接放行，用模型给出的 reply 兜底；
                        # 没有 reply 就生成一句通用接话，避免对话断在这里。
                        result = retry_result
                        vision_should = True
                        if str(retry_result.get("last_message_sender") or "").strip().lower() != "other":
                            # 归属仍被认错，这里改回对方，避免上游按「我方末条」处理
                            result["last_message_sender"] = "other"
                        if not str(retry_result.get("reply") or "").strip():
                            result["reply"] = "嗯嗯，你说～"
                        print("[策略] 末条为对方极短消息，强制放行不再判无需回复")
                    else:
                        print("[策略] 重试仍未通过，维持无需回复判定")
                except Exception as retry_err:
                    print(f"[策略] 低温重试失败，维持原判定: {retry_err}")
            out: Dict[str, Any] = {
                "should_reply": vision_should,
                "reply": result.get("reply", ""),
                "last_message_sender": result.get("last_message_sender", "unknown"),
                "last_message_side": result.get("last_message_side", "unknown"),
                "last_message_text": result.get("last_message_text", ""),
            }
            if reply_only_mode:
                # 派生值：有内容=要回。保证不会出现「should_reply=true 但 reply 为空」
                # 这种让下游发空消息的矛盾组合。
                out["should_reply"] = bool(str(out["reply"] or "").strip())
                out["reply_only_mode"] = True
            if cg:
                out["goal_reached"] = bool(result.get("goal_reached", False))
                out["handoff_message"] = (result.get("handoff_message") or "").strip()
            else:
                out["goal_reached"] = False
                out["handoff_message"] = ""
            return out

        except json.JSONDecodeError as e:
            err = str(e)
            print(f"[策略] 视觉回复JSON解析失败: {err}")
            return {
                "should_reply": False,
                "reply": "",
                "goal_reached": False,
                "handoff_message": "",
                "error": err,
                "api_should_stop": False,
                "api_user_message": "",
            }
        except Exception as e:
            err = str(e)
            print(f"[策略] 生成回复失败（不会发送到微信）: {err}")
            return {
                "should_reply": False,
                "reply": "",
                "goal_reached": False,
                "handoff_message": "",
                "error": err,
                "api_should_stop": is_api_stop_error(e),
                "api_user_message": api_error_user_message(e) if is_api_stop_error(e) else "",
            }

    def _build_system_prompt(
        self,
        strategy_text: str,
        gender: str,
        current_time_str: str,
        chat_goal: Optional[str] = None,
        channel: str = "vision",
        contact_display_name: Optional[str] = None,
    ) -> str:
        """构建系统提示：channel 为 vision（截图）或 text（纯文字记录）。"""
        cg = (chat_goal or "").strip()
        goal_block = ""
        is_vision = (channel or "vision").strip().lower() == "vision"
        json_fields = """- "should_reply": boolean（仅供参考，程序不据此拦截；真正决定发不发的是 reply 有没有内容）
- "reply": string（要发给对方的话；不想发就留空字符串，写成内容就会被发出去）
- "last_message_sender": "me" | "other" | "unknown"（必须先判断最后一条可见消息是谁发的）
- "last_message_side": "left" | "right" | "unknown"（最后一条可见消息所在方向）
- "last_message_text": string（最后一条消息的原文；非文字内容可填 "[图片: 描述]"、"[表情: 描述]" 等）"""
        if cg:
            goal_block = f"""

【本联系人聊天目标（操作员设定）】：
{cg}

【目标未达成时的回复结构（须遵守）】
- chat_strategy_config.json / Word 表单中的【策略要求】是最高优先级，不是参考材料；必须按它的阶段和目的生成回复。
- 聊天目标是中等优先级：要始终知道目标，但只有在上下文自然时推进；不要为了目标牺牲真人感、节奏和对方当下话题。
- 当存在待回复语境（例如对方刚提问、刚陈述、或明显在等你接话）时，应在 reply 里写出回复；除非隐私、安全或策略明确要求不回复（此时 reply 留空）。
- 「收到」「ok」「好的」「借你吉言」「借你的吉言」「接你吉言」「接你的吉言」「那必须」「哈哈」这类短回应，如果是对方刚发来的左侧消息，不等于对话结束；它通常是在接你的话，应写出回复。
- 只要你写了 reply：优先正常接话（回应对方提问/情绪/话题），在自然不突兀时顺势往目标推一小步；不要只接梗、只玩笑、只情绪回应。
- 如果聊天目标是线下约会/见面/吃饭，对方短回应没有拒绝时，避免只回「好嘞」「收到」「借光」这类原地打转的话；可以自然给出低压力邀约，例如「等你忙完，找天一起吃个饭/喝杯奶茶/出去逛逛」。
- 邀约形式必须与目标性质一致：目标是商务/项目/合作/开会/演示时，只能约「会议、电话、线上会、当面过方案」等商务形式，严禁改成吃饭、喝奶茶、出去玩、边吃边聊等私人约会式邀约（会显得不专业且偏离目标）。上面那条吃饭示例仅适用于私人约会类目标。
- 禁止连续围绕同一个比喻、玩笑或表演感话题空转；可以接梗半句，但整体仍要像真人聊天，并优先遵守策略节奏。
- 若主要需你方主动推进、而对方暂无新增信息：可自然发起与目标相关的一问，仍须口语化、不施压，并遵守隐私约束。
- 能从截图或记录中已读到的信息不得重复追问。

【目标已达成时的处理】若从截图或记录可较有把握判断目标已达成，goal_reached 设为 true，handoff_message 简要说明理由即可。
但不应因此就不回消息：对方发了新消息依然正常回复，只是不用再刻意推进目标了。"""
            json_fields += """
- "goal_reached": boolean
- "handoff_message": string（给操作员看的简短说明；未触发转人工时可为 ""）"""

        ch = (channel or "vision").strip().lower()
        if ch == "text":
            intro = "你是一个聊天助手，根据「文字形式的聊天记录」判断是否回复以及生成合适的回复内容。"
            task1 = "按时间顺序理解用户提供的聊天记录（发送者、内容、时间戳若有则参考）。"
        else:
            intro = "你是一个聊天助手，专门负责根据聊天截图判断是否回复以及生成合适的回复内容。"
            task1 = "仔细分析聊天截图，提取对话历史（包括发送者、内容、时间戳）。"

        contact_line = ""
        cdn = (contact_display_name or "").strip()
        if cdn:
            contact_line = f"\n【当前对话对象昵称】：{cdn}"

        privacy = format_privacy_guardrails_block(self._strategy_user_config_path)
        privacy_sec = f"\n{privacy}\n" if privacy else ""
        return f"""{intro}

【当前时间】：{current_time_str}{contact_line}

【策略要求】：
{strategy_text}
{privacy_sec}
【对方性别】：{gender}
{goal_block}

【极短消息规则（必须遵守）】
- 对方只发一个标点符号（「。」「？」「，」「~」等）、单个字（「嗯」「哦」「啊」「好」「是」等）或单个表情，都算一条真实的对方消息，不能当成噪声、界面残留或「没有内容」而忽略。
- 这类消息通常表示对方在等你接话、回应刚才的话题或收尾，**必须写出一句自然、口语化的简短回应**（reply 留空就等于不回，会被当成漏回）；不要复读这个标点本身，也不要只回一个标点。
- 判断最后一条消息归属以气泡方向和位置为准：左侧白色气泡就是对方，哪怕里面只有一个句号。
- 只有最后一条确实是我方发出的（右侧气泡），才允许 reply 留空。

【短肯定的上下文联动（必须遵守）】
- 对方的「嗯」「嗯嗯」「好」「好的」「行」「可以」「OK」「对」这类极短肯定，含义取决于我方上一句是什么：
  · 我方上一句是提问、邀约、征求同意或让对方确认（含「吗」「？」「方便吗」「要不要」「哪天」「一起去」等）→ 这是**同意/答应**，必须写出顺着聊天目标推进下一步（确认时间、地点、材料、联系方式）的回复内容，不要回一句「好」「收到」把球踢回去，也不要判定为敷衍。
  · 我方上一句只是一段陈述、闲聊或已经收尾 → 才是敷衍/收尾，可以不回（reply 留空）。
- 对方答应后至少要往前推进一步，例如把「行」变成「那定周六下午三点，我在XX等你」这种可执行的下一步。

【发送规则（最高优先，覆盖以上所有说法）】
- 是否发送只看 reply 有没有内容：reply 写了内容就一定发出去，reply 是空字符串就不发。
- 因此「不想回」只能用 reply 留空来表达；任何一句写在 reply 里的客套、收尾、礼貌性回应都会被当作真的发送。
- should_reply 字段只是留给你自己标注，程序不会因为它是 false 而不发。
- 例外：对方明确表示不想再聊（「不要再给我发消息了」「别发了」「不感兴趣」「不要打扰我」等）时，**不是沉默场景**：
  必须在 reply 里回一句简短、体面的收尾语（例如「好的，那就不打扰您了。」），只说一句，不复述、不辩解、不追问、不再推销。

【任务】：
1. {task1}
2. 根据策略要求，判断是否应该回复。
3. 如果决定回复，生成回复内容。
4. 如果决定不回复，reply 字段可为空字符串。
5. reply 只能是最终要发送给对方的话，不能包含分析过程、策略说明、括号注释或旁白。

【输出格式】：
必须输出严格的JSON对象，包含以下字段：
{json_fields}"""

    def extract_chat_history_from_image(
        self,
        user_id: str,
        image_url: str,
    ) -> Dict[str, Any]:
        """从聊天区截图提取结构化记录；图片/表情用文字描述替换。"""
        system_prompt = """
你是一个微信聊天截图转写助手，只负责提取聊天记录，不负责判断怎么回复。

请从提供的右侧聊天消息区截图中，按从上到下顺序提取所有可见消息。
左侧/白色/头像在左边 = 对方 other；右侧/绿色/头像在右边 = 我方 me。
忽略输入框、工具栏、窗口标题、光标、滚动条、时间变化等非消息内容。

输出严格 JSON：
{
  "messages": [
    {
      "sender": "me" | "other" | "unknown",
      "side": "left" | "right" | "unknown",
      "content_type": "text" | "image" | "sticker" | "link" | "file" | "mixed" | "unknown",
      "content": "文本内容；若是图片/表情/文件/链接，写成 [图片: 画面描述]、[表情: 表情描述]、[文件: 文件描述] 等",
      "timestamp": "截图中这一条附近可见的时间；没有则为 null"
    }
  ],
  "last_message_sender": "me" | "other" | "unknown",
  "last_message_side": "left" | "right" | "unknown",
  "last_message_text": "最后一条可见消息的 content",
  "confidence": 0.0到1.0
}

重要：
- 不要编造截图外的历史。
- 非文字内容必须替换为简短文字描述，不要只写“图片”。
- 如果看不清文字，content 写 "[看不清: 简短说明]"。
- 不要输出回复建议、分析过程或解释。
"""
        user_message = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": "请把截图中的可见聊天记录提取成 JSON，图片和表情用文字描述替换。"}
            ]
        }

        max_attempts = 1
        last_error = ""
        for attempt in range(max_attempts):
            try:
                response = self._call_api(
                    user_id=user_id,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        user_message,
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=800,
                )
                raw = response["choices"][0]["message"]["content"]
                result = _robust_json_parse(raw)
                if not isinstance(result, dict):
                    last_error = "模型输出不是JSON对象"
                    if attempt < max_attempts - 1:
                        print(f"  [AI] 提取JSON解析失败(第{attempt+1}次): {last_error}，重试中...")
                        continue
                    return {"error": last_error, "messages": []}
                msgs = result.get("messages")
                if not isinstance(msgs, list):
                    result["messages"] = []
                return result
            except json.JSONDecodeError as e:
                last_error = str(e)
                if attempt < max_attempts - 1:
                    print(f"  [AI] 提取JSON解析失败(第{attempt+1}次): {e}，重试中...")
                    continue
                return {
                    "error": last_error,
                    "messages": [],
                    "api_should_stop": False,
                    "api_user_message": "",
                }
            except Exception as e:
                if is_api_stop_error(e):
                    return {
                        "error": str(e),
                        "messages": [],
                        "api_should_stop": True,
                        "api_user_message": api_error_user_message(e),
                    }
                last_error = str(e)
                if attempt < max_attempts - 1:
                    print(f"  [AI] 提取失败(第{attempt+1}次): {e}，重试中...")
                    continue
        return {
            "error": last_error,
            "messages": [],
            "api_should_stop": False,
            "api_user_message": "",
        }


# ============================================================================
# 命令行接口
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="基于图像的聊天回复生成器")
    parser.add_argument("--user", required=True, help="用户ID")
    parser.add_argument("--image", required=True, help="聊天截图URL")
    parser.add_argument("--gender", default="未知", help="对方性别")
    parser.add_argument("--user-config", default="user_config.json", help="用户API配置文件")
    parser.add_argument("--chat-goal", default=None, help="聊天目标（非空时输出 goal_reached / handoff_message）")
    args = parser.parse_args()

    strategy = VisualChatStrategy(args.user_config)
    cg = (args.chat_goal or "").strip() or None
    result = strategy.generate_reply_from_image(
        user_id=args.user,
        image_url=args.image,
        gender=args.gender,
        long_cycle_monitor=False,
        chat_goal=cg,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
