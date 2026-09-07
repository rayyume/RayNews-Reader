"""RayNews AI Service — unified interface for OpenAI-compatible and Claude-compatible APIs."""

import os
import json
import requests
import re
import uuid
from collections import defaultdict
from typing import Optional
from urllib.parse import urlsplit

from network_safety import _ALLOW_PRIVATE_AI_ENDPOINTS, safe_post
from source_categories import CATEGORY_NAMES, CATEGORY_ORDER, clamp_weighted, local_short_source_name


# Title-processing chats produce a short answer but can trigger a lot of *hidden*
# reasoning on "thinking" models (common on gateways like opencode.ai/zen). With too
# small a budget the reasoning eats every token and the model returns empty content,
# surfacing as "empty AI title summary". Give these calls enough room to finish; tune
# via AI_TITLE_MAX_TOKENS if a heavier reasoning model still comes back empty.
TITLE_MAX_TOKENS = max(200, int(os.environ.get("AI_TITLE_MAX_TOKENS", "4096")))
SOURCE_CLASSIFY_MAX_TOKENS = max(
    200, int(os.environ.get("AI_SOURCE_CLASSIFY_MAX_TOKENS", "2048"))
)
_MAX_TOKENS_ACTION_HINT = "请增大 max_tokens"


def _redact_api_error(value: str, *known_secrets: str) -> str:
    """Return a compact provider error with credentials removed."""
    text = " ".join(str(value or "").split())
    for secret in known_secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = re.sub(
        r"(?i)\b(?:proxy-)?authorization\s*:\s*(?:bearer\s+)?[^\s,;]+",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        text,
    )
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)(?:api[_-]?key|x-api-key|access[_-]?token|token|secret|password|key)"
        r"\s*(?:=|:)\s*(?:[\"']?)[^\s,;&}\]\"']+",
        "[redacted]",
        text,
    )
    return text


def validate_ai_endpoint_base_url(endpoint: str) -> str:
    """Reject endpoint suffixes that would be malformed or leak credentials.

    Public-address validation remains the persistence boundary's responsibility;
    this syntax-only guard is also applied when loading legacy persisted values so
    no request can be made with a query or fragment in the configured base URL.
    """
    try:
        parsed = urlsplit(endpoint)
    except (TypeError, ValueError):
        raise ValueError("AI endpoint must be a base HTTP(S) URL") from None
    if parsed.query or parsed.fragment:
        raise ValueError("AI endpoint must be a base HTTP(S) URL")
    return endpoint


def _empty_ai_content_error(finish_reason, has_reasoning: bool, max_tokens: int) -> str:
    """Actionable message for a well-formed API response that carries no usable text —
    almost always a reasoning model whose hidden thinking exhausted max_tokens."""
    hint = ""
    if has_reasoning or finish_reason in ("length", "max_tokens"):
        hint = (
            f"；该模型疑似为推理(thinking)模型，隐藏推理耗尽了 max_tokens={max_tokens} 的预算导致正文为空。"
            f"{_MAX_TOKENS_ACTION_HINT} 或改用非推理模型。"
        )
    return f"AI 返回空内容（finish_reason={finish_reason}{hint}）"


def _normalize_cjk_quotes(text: str) -> str:
    """Convert ASCII straight quotes and Unicode curly quotes to Chinese corner brackets 「」 in CJK context.

    Replaces paired "…", \u201c…\u201d (Unicode curly double) and, for content
    containing CJK characters, paired '…' and \u2018…\u2019 (Unicode curly single)
    with 「…」.  Latin-only apostrophes and quotes (e.g. "India's") are left untouched.
    """
    # Paired double quotes — ASCII and Unicode curly
    text = re.sub(r'"([^"]+)"', r'「\1」', text)
    text = re.sub('\u201c([^\u201d]+)\u201d', r'「\1」', text)
    # Paired single quotes — ASCII and Unicode curly (only in CJK context)
    text = re.sub(
        r"'([^']+)'",
        lambda m: f'「{m.group(1)}」'
        if re.search(r'[\u4e00-\u9fff]', m.group(1))
        else m.group(0),
        text,
    )
    text = re.sub(
        '\u2018([^\u2019]+)\u2019',
        lambda m: f'「{m.group(1)}」'
        if re.search(r'[\u4e00-\u9fff]', m.group(1))
        else m.group(0),
        text,
    )
    return text


# ─── Token-aware text truncation ─────────────────────────────
# Conservative char→token estimate: 1 token ≈ 4 ASCII chars or ≈ 2 CJK chars
# We use 3 chars/token as a safe midpoint for mixed content.
_CHARS_PER_TOKEN = 3
_MAX_INPUT_CHARS = 12_000  # ≈ 4000 tokens — fits most models' input comfortably


def _token_aware_truncate(text: str, max_chars: int = _MAX_INPUT_CHARS) -> str:
    """Strip HTML and build a prioritized excerpt: title → opening → key paragraphs → ending.

    Preserves the most informative parts of an article within a token-aware budget.
    Returns clean plain text (no HTML).
    """
    # Strip HTML tags
    plain = re.sub(r'<[^>]+>', '\n', text)
    plain = re.sub(r'\s*\n\s*', '\n', plain)
    plain = plain.strip()

    if not plain:
        return ""

    # If already within budget, return as-is
    if len(plain) <= max_chars:
        return plain

    # Split into paragraphs (non-empty lines)
    paragraphs = [p.strip() for p in plain.split('\n') if p.strip()]
    if not paragraphs:
        return plain[:max_chars]

    budget = max_chars
    parts = []

    # 1) First paragraph (usually the lede / most important) — keep fully
    first = paragraphs[0]
    if len(first) < budget * 0.6:  # don't let a single paragraph eat >60%
        parts.append(first)
        budget -= len(first)
        para_start = 1
    else:
        # First paragraph is huge — take its truncated form
        parts.append(first[:budget // 2])
        budget -= len(parts[0])
        para_start = 1

    # 2) Last paragraph (conclusion) — keep if budget allows
    last = paragraphs[-1]
    if len(paragraphs) > 2 and len(last) + 100 <= budget:
        parts.append(last)
        budget -= len(last) + 2  # +2 for separator
        para_end = len(paragraphs) - 1
    else:
        para_end = len(paragraphs)

    # 3) Middle — scan for key paragraphs (bold indicators, caps-start, lists)
    for p in paragraphs[para_start:para_end]:
        if budget <= 100:
            break
        is_key = (
            p.startswith('**') or  # bold/highlighted
            p.startswith('•') or p.startswith('-') or p.startswith('* ') or  # lists
            p.startswith('#') or  # markdown headers
            (len(p) > 15 and p[0].isupper() and p[1].isalpha())  # likely a heading
        )
        if is_key and len(p) + 100 <= budget:
            parts.append(p)
            budget -= len(p) + 2
        elif not is_key and budget > 300:
            # Non-key paragraphs get a condensed allocation
            max_p = min(len(p), budget // 2)
            if max_p > 60:
                parts.append(p[:max_p] + ('…' if max_p < len(p) else ''))
                budget -= max_p + 2

    # 4) If we still have budget, add more middle content from top
    if budget > 200:
        for p in paragraphs[para_start:para_end]:
            if budget <= 50:
                break
            if p not in parts and len(p) + 50 <= budget:
                # Shorten aggressively
                take = min(len(p), budget)
                parts.append(p[:take] + ('…' if take < len(p) else ''))
                budget -= take + 2

    return '\n\n'.join(parts)


class AIService:
    """Unified AI service supporting both OpenAI format and Claude format APIs.

    Use cases: DeepSeek (OpenAI), MiniMax (both), Anthropic Claude (native),
    OpenAI (native), Ollama (OpenAI).
    """

    def __init__(self, api_key: str, endpoint: str, model: str,
                 provider_type: str = "openai"):
        self.api_key = api_key
        self.endpoint = validate_ai_endpoint_base_url(endpoint).rstrip("/")
        self.model = model
        self.provider_type = provider_type  # 'openai' or 'claude'
        self.request_timeout = int(os.environ.get("AI_REQUEST_TIMEOUT_SECONDS", "300"))
        self._session_id = str(uuid.uuid4())

    def _opencode_routing_headers(self) -> dict[str, str]:
        parsed = urlsplit(self.endpoint)
        path = parsed.path.lower().rstrip("/")
        if (
            parsed.hostname == "opencode.ai"
            and (path == "/zen/go" or path.startswith("/zen/go/"))
        ):
            return {
                "User-Agent": "RayNews-Reader/1.0",
                "x-opencode-session": self._session_id,
            }
        return {}

    def _is_deepseek(self) -> bool:
        return "deepseek.com" in (self.endpoint or "").lower()

    def _is_deepseek_model(self) -> bool:
        return "deepseek" in (self.model or "").lower()

    def _provider_output_cap(self, requested: int, purpose: str = "default") -> int:
        if not self._is_deepseek():
            return requested
        if purpose == "daily_final":
            cap = int(os.environ.get("AI_DAILY_DEEPSEEK_FINAL_MAX_TOKENS", "3600"))
        elif purpose == "daily_continue":
            cap = int(os.environ.get("AI_DAILY_DEEPSEEK_CONTINUE_MAX_TOKENS", "1200"))
        else:
            cap = int(os.environ.get("AI_DEEPSEEK_MAX_TOKENS", "4000"))
        return min(requested, cap)

    def _format_api_error(self, resp: requests.Response) -> str:
        body = (resp.text or "").strip()
        detail = ""
        if body:
            try:
                data = resp.json()
                err = data.get("error") if isinstance(data, dict) else None
                if isinstance(err, dict):
                    detail = err.get("message") or err.get("type") or json.dumps(err, ensure_ascii=False)
                elif isinstance(err, str):
                    detail = err
                elif isinstance(data, dict):
                    detail = data.get("message") or json.dumps(data, ensure_ascii=False)
            except Exception:
                detail = body
        if not detail:
            detail = resp.reason or "empty response"
        detail = _redact_api_error(detail, self.api_key)
        if len(detail) > 500:
            detail = detail[:500] + "..."
        return f"AI API HTTP {resp.status_code}: {detail}"

    # ─── OpenAI-compatible API call ──────────────────────────

    def _call_openai(self, messages: list, max_tokens: int = 2000,
                     temperature: float = 0.3) -> str:
        url = f"{self.endpoint}/chat/completions"
        # Handle both {endpoint}/v1/chat/completions and bare {endpoint}/chat/completions
        if "/v1" not in url and "/v1" not in self.endpoint:
            url = f"{self.endpoint}/v1/chat/completions"

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._is_deepseek_model():
            body["thinking"] = {"type": "disabled"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._opencode_routing_headers())
        try:
            resp = safe_post(
                url,
                headers=headers,
                json=body,
                timeout=(30, self.request_timeout),
                allow_private=_ALLOW_PRIVATE_AI_ENDPOINTS,
            )
        except requests.exceptions.ReadTimeout as e:
            raise TimeoutError(
                f"AI 服务响应超时（超过 {self.request_timeout} 秒），请稍后重试或减少日报文章数量"
            ) from e
        if not resp.ok:
            raise RuntimeError(self._format_api_error(resp))
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if not content.strip():
            # A 200 with empty content isn't a usable answer — don't hand callers ""
            # (which they'd log as a bare "empty" and retry forever). Raise with the
            # finish_reason so the real cause is visible in logs.
            raise RuntimeError(_empty_ai_content_error(
                choice.get("finish_reason"),
                bool(message.get("reasoning_content") or message.get("reasoning")),
                max_tokens,
            ))
        return content

    # ─── Claude-compatible API call ──────────────────────────

    def _call_claude(self, messages: list, max_tokens: int = 2000,
                     temperature: float = 0.3) -> str:
        url = f"{self.endpoint}/messages"
        # Handle both {endpoint}/v1/messages and bare {endpoint}/messages
        if "/v1" not in url and "/v1" not in self.endpoint:
            url = f"{self.endpoint}/v1/messages"

        # Convert OpenAI-style messages to Anthropic format
        system = ""
        claude_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                claude_messages.append({
                    "role": "assistant" if msg["role"] == "assistant" else "user",
                    "content": msg["content"],
                })

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": claude_messages,
            "temperature": temperature,
        }
        if system:
            body["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        headers.update(self._opencode_routing_headers())
        try:
            resp = safe_post(
                url,
                headers=headers,
                json=body,
                timeout=(30, self.request_timeout),
                allow_private=_ALLOW_PRIVATE_AI_ENDPOINTS,
            )
        except requests.exceptions.ReadTimeout as e:
            raise TimeoutError(
                f"AI 服务响应超时（超过 {self.request_timeout} 秒），请稍后重试或减少日报文章数量"
            ) from e
        if not resp.ok:
            raise RuntimeError(self._format_api_error(resp))
        data = resp.json()
        blocks = data.get("content") or []
        text = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text.strip():
            raise RuntimeError(_empty_ai_content_error(
                data.get("stop_reason"),
                any(isinstance(b, dict) and b.get("type") == "thinking" for b in blocks),
                max_tokens,
            ))
        return text

    # ─── Unified chat ────────────────────────────────────────

    def chat(self, messages: list, max_tokens: int = 2000,
             temperature: float = 0.3) -> str:
        if self.provider_type == "claude":
            return self._call_claude(messages, max_tokens, temperature)
        return self._call_openai(messages, max_tokens, temperature)

    def _chat_for_task(self, messages: list, max_tokens: int,
                       temperature: float, max_tokens_env: str) -> str:
        """Call ``chat`` without widening its override contract.

        Task methods decorate only the actionable empty-output error; provider
        errors and custom ``chat`` overrides otherwise keep their exact behavior.
        """
        try:
            return self.chat(messages, max_tokens=max_tokens, temperature=temperature)
        except RuntimeError as exc:
            message = str(exc)
            if (
                message.startswith("AI 返回空内容（")
                and _MAX_TOKENS_ACTION_HINT in message
            ):
                message = message.replace(
                    _MAX_TOKENS_ACTION_HINT,
                    f"{_MAX_TOKENS_ACTION_HINT}（可设环境变量 {max_tokens_env}）",
                    1,
                )
                raise RuntimeError(message) from exc
            raise

    # ─── Summarize ───────────────────────────────────────────

    def summarize(self, article_text: str, title: str = "") -> str:
        truncated = _token_aware_truncate(article_text)
        prompt = "请为以下文章生成一个简洁的中文摘要，200字以内。"
        if title:
            prompt = f"文章标题：{title}\n\n{prompt}"
        messages = [
            {"role": "system",
             "content": "你是一个专业的新闻摘要助手。请用简洁的语言概括文章核心内容。"},
            {"role": "user",
             "content": f"{prompt}\n\n文章内容：\n{truncated}"},
        ]
        return self.chat(messages)

    def classify_source(self, source: str, titles: list[str] | None = None,
                        domains: list[str] | None = None) -> dict:
        """Classify a source and shorten its display name using the user's AI API.

        Args:
            source: Raw source name from the database.
            titles: Up to 8 recent article titles for context.
            domains: Domain names extracted from article links for stronger signal.
        """
        titles = [t for t in (titles or []) if t][:8]
        category_lines = "\n".join(f"- {key}: {CATEGORY_NAMES[key]}" for key in CATEGORY_ORDER)
        title_lines = "\n".join(f"{idx + 1}. {title}" for idx, title in enumerate(titles)) or "无"
        domain_lines = ", ".join(domains[:10]) if domains else "无"
        fallback_label = local_short_source_name(source)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是新闻来源整理助手。你的任务不是改写来源名，而是缩写来源名："
                    "去掉 Telegram Channel、频道、表情符号、装饰性后缀等无效内容，"
                    "只保留最具代表性的来源名称。分类只能从给定类别中选择。"
                    "域名是判断来源性质的强信号，请优先根据域名确定分类。"
                    "必须只输出 JSON，不要输出解释。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"来源原名：{source}\n\n"
                    f"相关域名：{domain_lines}\n\n"
                    f"最近文章标题：\n{title_lines}\n\n"
                    f"可选分类：\n{category_lines}\n\n"
                    "输出 JSON 格式：\n"
                    "{\"category\":\"News|Tech|Biz|Info\",\"label\":\"缩写后的来源名\",\"confidence\":0.0,\"reason\":\"简短原因\"}\n\n"
                    "规则：\n"
                    "1. category 必须是 News、Tech、Biz、Info 之一。\n"
                    "2. 域名是判断分类的最强信号。例如 zaobao.com → News，github.com → Tech，gelonghui.com → Biz。\n"
                    "3. label 是缩写，不是改写；如果原名已经简洁，保持原名。\n"
                    "4. label 按 ASCII=1、中文和其他非 ASCII=2 计算，长度必须不超过 20。\n"
                    "5. 示例：竹新社 - Telegram Channel -> 竹新社。\n"
                    "6. 示例：科技圈🎗在花频道📮 - Telegram Channel -> 在花科技圈。"
                ),
            },
        ]
        raw = self._chat_for_task(
            messages,
            max_tokens=SOURCE_CLASSIFY_MAX_TOKENS,
            temperature=0.1,
            max_tokens_env="AI_SOURCE_CLASSIFY_MAX_TOKENS",
        ).strip()
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise ValueError("AI source classification did not return JSON")
        data = json.loads(match.group(0))
        category = data.get("category")
        if category not in CATEGORY_ORDER:
            category = "Info"
        label = clamp_weighted(data.get("label") or fallback_label, 20)
        return {
            "category": category,
            "label": label or fallback_label,
            "confidence": data.get("confidence"),
            "reason": data.get("reason") or "",
        }

    # ─── Translate (bilingual) ───────────────────────────────

    def translate(self, article_text: str, title: str = "") -> str:
        truncated = _token_aware_truncate(article_text)
        prompt = ("请将以下文章逐段翻译为中文。\n\n"
                  "格式要求（非常重要）：\n"
                  "1. 将文章按段落拆分，每段原文后面紧跟该段的中文翻译\n"
                  "2. 原文段和译文段之间用『||』分隔\n"
                  "3. 段与段之间用两个换行分隔\n"
                  "4. 输出纯文本，不要任何 HTML 标签（不要 <b> <u> <br/> 等）\n"
                  "5. 如果原文包含加粗或斜体，只保留纯文字含义\n\n"
                  "示例格式：\n"
                  "Today is Tuesday.||今天是星期二。\n\n"
                  "The weather is sunny.||天气晴朗。\n\n"
                  "Please output paragraph by paragraph exactly as shown above.")
        if title:
            prompt = f"文章标题：{title}\n\n{prompt}"
        messages = [
            {"role": "system",
             "content": "你是一个专业的翻译助手。逐段翻译，每段原文后紧跟该段译文，用『||』分隔。只输出纯文本，不要 HTML 标签。"},
            {"role": "user",
             "content": f"{prompt}\n\n文章内容：\n{truncated}"},
        ]
        return self.chat(messages, max_tokens=4000)

    @staticmethod
    def _retry_feedback_line(feedback: str) -> str:
        """A user-message suffix used when re-asking after a rejected result,
        so the retry has a reason to produce something different instead of
        re-emitting the same output at low temperature."""
        if not feedback:
            return ""
        return (
            f"\n\n注意：上一次输出存在问题（{feedback}），"
            "请重新输出一个完整、成对标点配对齐全、语义完整的标题。"
        )

    def translate_title(self, title: str, target_lang: str = "zh-CN",
                        feedback: str = "", temperature: float = 0.2) -> str:
        """Translate a single article title. `feedback` (a prior failure
        reason) triggers a corrective re-ask; callers raise `temperature` on
        retries so a low-temp model doesn't just repeat the same bad output."""
        lang_name = "中文" if "zh" in target_lang else target_lang
        messages = [
            {
                "role": "system",
                "content": f"你是一个专业的新闻标题翻译助手。将标题翻译为{lang_name}，只输出译文。",
            },
            {
                "role": "user",
                "content": (
                    "请翻译以下新闻标题。要求：\n"
                    "1. 只输出翻译后的标题，不要解释。\n"
                    "2. 保留专有名词、公司名、产品名的通用写法。\n"
                    "3. 不要添加原文没有的信息。\n\n"
                    f"标题：{title}"
                    + self._retry_feedback_line(feedback)
                ),
            },
        ]
        return _normalize_cjk_quotes(self._chat_for_task(
            messages,
            max_tokens=TITLE_MAX_TOKENS,
            temperature=temperature,
            max_tokens_env="AI_TITLE_MAX_TOKENS",
        ).strip())

    def _title_summary_system_prompt(self, max_chars: int, min_chars: int) -> str:
        return (
            "# Role\n"
            "你是一个资深的新闻编辑，擅长用极简、准确的语言提炼新闻核心。\n\n"
            "# Rule of 3 Elements (三要素融合标准)\n"
            "生成的单一标题必须在一句话中隐性包含以下三个核心要素：\n"
            "1. 核心主体：事件的主角（谁/哪个机构）。\n"
            "2. 核心事实：最新发生的最重大动作（做了什么）。\n"
            "3. 关键结果/数字：最能体现事件影响的细节或数据。\n\n"
            "# Constraints\n"
            f"- 字数尽量控制在 {min_chars}-{max_chars} 字之间，拒绝过短；"
            "如果为了保留关键信息、避免语义割裂或标点不完整，超过这个字数上限也可以，"
            "但不要为了凑字数而堆砌无关细节。\n"
            "- 必须保留原标题的核心主体（人物/机构/产品等专有名词）和核心动作，"
            "禁止只保留消息来源或引述框架（如「据FT报道」「知情人士称」等）而丢掉主体和事实本身。\n"
            "- 拒绝结构拆分：只需输出一行最终的标题，严禁带有「引题」「正题」「副题」等标签。\n"
            "- 拒绝前言后语：禁止输出「这是为你生成的标题：」等任何解释性废话。\n"
            "- 客观准确：严格基于原文事实，严禁夸大、魔改或使用震惊体。\n"
            "- 如有标点符号需准确：标题中如有《》「」等成对出现符号，需确保标点符号的完整，禁止仅出现一边的符号。\n\n"
            "只输出一行最终标题的纯文本，不要 JSON、解释、标签或代码块。"
        )

    def summarize_title(self, title: str, max_chars: int = 35, min_chars: int = 18,
                        feedback: str = "", temperature: float = 0.2) -> str:
        """Shorten a news title using a 3-element editing approach. Returns a
        plain-text title (not JSON): the previous JSON contract was brittle —
        an inner quote could break parsing and silently turn the raw payload
        into the "title". `feedback`/`temperature` drive corrective retries."""
        messages = [
            {
                "role": "system",
                "content": self._title_summary_system_prompt(max_chars, min_chars),
            },
            {
                "role": "user",
                "content": f"原标题：{title}" + self._retry_feedback_line(feedback),
            },
        ]
        return self._chat_for_task(
            messages,
            max_tokens=TITLE_MAX_TOKENS,
            temperature=temperature,
            max_tokens_env="AI_TITLE_MAX_TOKENS",
        ).strip()

    def translate_and_condense_title(self, title: str, target_lang: str = "zh-CN",
                                     max_chars: int = 30, min_chars: int = 18,
                                     feedback: str = "", temperature: float = 0.2) -> str:
        """One-shot translate + shorten for a title that is BOTH foreign and
        over-long. Doing it in a single call (instead of translate → notice
        it's still long → summarize) avoids a second AI round-trip and stops
        the on-screen title from visibly changing twice. Reuses the same
        3-element editing standard as summarize_title, with a translate step
        folded in. Returns plain text."""
        lang_name = "中文" if "zh" in target_lang else target_lang
        system = (
            f"你是一个资深的双语新闻编辑。先将标题准确翻译为{lang_name}，"
            "再按下述标准精炼为一个简洁完整的标题。\n\n"
            + self._title_summary_system_prompt(max_chars, min_chars)
            + "\n- 忠实于原文含义，保留专有名词、公司名、产品名的通用译法，不要添加原文没有的信息。"
        )
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"原标题（需翻译并精炼）：{title}" + self._retry_feedback_line(feedback),
            },
        ]
        return _normalize_cjk_quotes(
            self._chat_for_task(
                messages,
                max_tokens=TITLE_MAX_TOKENS,
                temperature=temperature,
                max_tokens_env="AI_TITLE_MAX_TOKENS",
            ).strip()
        )


    # Daily summary (layered)
    # Strategy:
    #   1. For each article, use existing AI summary if available, otherwise an excerpt.
    #   2. Group and cap articles before asking AI to select final candidates.
    #   3. Generate the final Markdown summary from the selected articles.
    # Returns {"summary": str, "stats": {...}}.

    def daily_summary(self, articles: list[dict]) -> dict:
        return self._daily_summary_v2(articles)

    def _daily_summary_v2(self, articles: list[dict]) -> dict:
        is_deepseek = self._is_deepseek()
        max_summary_chars = int(os.environ.get(
            "AI_DAILY_SUMMARY_CHARS",
            "260" if is_deepseek else "360",
        ))
        max_title_chars = int(os.environ.get(
            "AI_DAILY_TITLE_CHARS",
            "120" if is_deepseek else "160",
        ))
        max_articles_per_source = int(os.environ.get("AI_DAILY_MAX_ARTICLES_PER_SOURCE", "100"))
        target_items = int(os.environ.get("AI_DAILY_TARGET_ITEMS", "40"))
        min_items = int(os.environ.get("AI_DAILY_MIN_ITEMS", str(max(1, target_items - 5))))
        max_items = int(os.environ.get("AI_DAILY_MAX_ITEMS", str(target_items + 5)))
        max_daily_candidates = int(os.environ.get(
            "AI_DAILY_MAX_CANDIDATES",
            "110" if is_deepseek else "120",
        ))
        max_items_per_category = int(os.environ.get("AI_DAILY_MAX_ITEMS_PER_CATEGORY", "16"))
        min_candidates_per_category = int(os.environ.get("AI_DAILY_MIN_CANDIDATES_PER_CATEGORY", "10"))
        categories = ("政经新闻", "科技动态", "商业聚焦", "其他信息")

        def article_link(article: dict) -> str:
            date = article.get("date", "") or ""
            art_id = article.get("id", 0)
            if date and art_id:
                return f"https://news.rayyu.me/#/article/{date[2:]}-{art_id}"
            return ""

        def plain_text(text: str) -> str:
            text = re.sub(r"<[^>]+>", " ", text or "")
            return " ".join(text.split()).strip()

        def compact_text(text: str, limit: int) -> str:
            text = plain_text(text)
            if len(text) <= limit:
                return text
            clipped = text[:limit].rstrip(" ，,、；;：:")
            punct_positions = [clipped.rfind(p) for p in "。！？!?"]
            best = max(punct_positions)
            if best >= max(12, limit // 2):
                return clipped[:best + 1].strip()
            soft_breaks = (" ", "\t", "，", ",", "、", "；", ";", "：", ":")
            soft_best = max(clipped.rfind(p) for p in soft_breaks)
            if len(clipped) < len(text) / 2 and soft_best >= max(16, int(limit * 0.6)):
                return clipped[:soft_best].strip()
            return clipped.strip()

        invalid_summary_re = re.compile(
            r"(请补充|请提供|缺少.*正文|缺少.*文章|文章全文|无法.*摘要|无法生成|"
            r"不能生成|没有提供|仅为[“\"']?via|您提供的文章内容|需要摘要的文章|"
            r"200字以内|no content|\(no content\))",
            re.I,
        )

        def informative_length(text: str) -> int:
            text = re.sub(r"(via|出处|来源)\s*[:：]?", " ", text or "", flags=re.I)
            text = re.sub(r"[\W_]+", "", text, flags=re.U)
            return len(text)

        def is_invalid_digest_text(text: str, source: str = "") -> bool:
            value = plain_text(text)
            if not value:
                return True
            if invalid_summary_re.search(value):
                return True
            if re.fullmatch(r"(?:via|出处|来源)\s*[:：]?\s*[\w\u4e00-\u9fff .,\-·🎗📮]+", value, re.I):
                return True
            if source and value.strip(" ：:，,。 ") == source.strip():
                return True
            if value.lower() in {"via", "source", "no content", "(no content)"}:
                return True
            return informative_length(value) < 6

        category_keywords = {
            "政经新闻": (
                "央行", "美联储", "利率", "通胀", "gdp", "pmi", "就业", "财政",
                "关税", "政府", "政策", "总统", "选举", "经济", "外交", "制裁",
                "国会", "法院", "战争", "伊朗", "俄罗斯", "欧盟", "税",
            ),
            "科技动态": (
                "ai", "openai", "deepseek", "claude", "模型", "大模型", "芯片", "半导体",
                "科技", "人工智能", "机器人", "苹果", "微软", "谷歌", "英伟达", "算力",
                "安卓", "ios", "软件", "开发者", "卫星", "spacex",
            ),
            "商业聚焦": (
                "公司", "财报", "营收", "利润", "融资", "上市", "ipo", "并购", "收购",
                "裁员", "市场", "品牌", "电商", "商业", "投资", "基金", "股价", "估值",
                "订单", "销量", "银行", "外汇", "人民币", "黄金", "原油",
            ),
        }
        category_priority = {"政经新闻": 3, "科技动态": 2, "商业聚焦": 1, "其他信息": 0}
        importance_keywords = (
            "央行", "美联储", "利率", "通胀", "gdp", "pmi", "关税", "政策", "制裁",
            "选举", "财政", "财报", "营收", "利润", "融资", "ipo", "上市", "并购",
            "收购", "ai", "openai", "芯片", "半导体", "英伟达", "苹果", "微软",
            "spacex", "黄金", "原油", "人民币", "汇率",
        )

        def category_scores(title: str, text: str, source: str) -> dict[str, int]:
            haystack = f"{title} {text} {source}".lower()
            scores = {}
            for category, keywords in category_keywords.items():
                scores[category] = sum(1 for keyword in keywords if keyword.lower() in haystack)
            scores["其他信息"] = 0
            return scores

        def classify_article(title: str, text: str, source: str) -> str:
            scores = category_scores(title, text, source)
            category, score = max(
                scores.items(),
                key=lambda pair: (pair[1], category_priority.get(pair[0], 0)),
            )
            return category if score > 0 else "其他信息"

        def score_article(title: str, text: str, source: str, has_summary: bool) -> tuple[float, int]:
            haystack = f"{title} {text} {source}".lower()
            keyword_hits = sum(1 for keyword in importance_keywords if keyword.lower() in haystack)
            # info_score reflects how much an article has been "written up",
            # not how newsworthy it is: a breaking item that hasn't had time
            # to accumulate a long body reads the same as a low-importance
            # one. Count it once (not once per component) and keep the
            # has_summary bonus small, so short/fresh items aren't penalized
            # twice for the same trait.
            info_score = min(len(title) / 18, 3) + min(len(text) / 90, 4)
            quality_score = 2 if has_summary else 1
            importance_score = keyword_hits * 2
            if re.search(r"\d|%|亿元|亿美元|万亿|百分点|基点", haystack):
                importance_score += 1.5
            return quality_score + importance_score + info_score, keyword_hits

        def normalize_title_key(title: str) -> str:
            value = plain_text(title).lower()
            value = re.sub(r"[\s\"'“”‘’「」『』:：,，.。!！?？\-—_]+", "", value)
            return value[:80]

        def format_article_entry(article: dict, index: int) -> str:
            text_label = "内容摘要" if article.get("has_summary") else "标题线索"
            return "\n".join([
                f"{index}. ID：{article['id']}",
                f"来源：{article['source']}",
                f"建议分类：{article['category']}",
                f"关键词命中：{', '.join(article.get('keyword_hits') or []) or '无'}",
                f"标题：{article['title']}",
                f"{text_label}：{article['text']}",
                f"链接：{article.get('url', '')}",
            ])

        def select_local(items: list[dict], target: int | None = None) -> list[dict]:
            target = target or target_items
            sorted_items = sorted(items, key=lambda e: e.get("score", 0), reverse=True)
            chosen = []
            chosen_ids = set()
            per_category = defaultdict(int)

            # Keep dynamic allocation, but give each non-empty category one
            # representative when the target has enough room. This avoids a
            # single hot category hiding all other meaningful news.
            for category in categories:
                category_items = [e for e in sorted_items if e.get("category") == category]
                if not category_items:
                    continue
                item = category_items[0]
                if item.get("id") in chosen_ids:
                    continue
                chosen.append(item)
                chosen_ids.add(item.get("id"))
                per_category[category] += 1
                if len(chosen) >= min(target, max_items):
                    return chosen

            for item in sorted_items:
                if len(chosen) >= max_items:
                    break
                category = item.get("category") or "其他信息"
                if item.get("id") in chosen_ids:
                    continue
                if per_category[category] >= max_items_per_category and len(chosen) >= min_items:
                    continue
                chosen.append(item)
                chosen_ids.add(item.get("id"))
                per_category[category] += 1
                if len(chosen) >= target and len(chosen) >= min_items:
                    break
            return chosen

        def fallback_daily_summary(items: list[dict]) -> str:
            chosen = select_local(items)

            lines = []
            for category in categories:
                category_items = [e for e in chosen if e["category"] == category]
                if not category_items:
                    continue
                lines.append(f"## {category}")
                for idx, e in enumerate(category_items, 1):
                    title = compact_text(e.get("title", ""), 42)
                    text = compact_text(e.get("text", ""), 90)
                    if text and text[-1] not in "。！？.!?":
                        text = text.rstrip(" ，,、；;：:") + "。"
                    lines.append(f"{idx}. **{title}：** {text} [🔗]({e.get('url', '')})")
            return "\n".join(lines) if lines else "今日无高质量新闻可总结。"

        def summary_looks_truncated(text: str) -> bool:
            stripped = (text or "").strip()
            if not stripped:
                return True
            linked_items = len(re.findall(r"^\s*\d+\.\s+.*?\[🔗\]\(", stripped, flags=re.M))
            if linked_items >= min_items and stripped.count("**") % 2 == 0:
                return False
            if "…" in stripped or "..." in stripped:
                return True
            last_line = stripped.splitlines()[-1].strip()
            if stripped.count("**") % 2 != 0:
                return True
            if last_line.startswith("-") and "[🔗](" not in last_line:
                return True
            if re.search(r"\*\*[^*\n]*$", stripped):
                return True
            return False

        def cleanup_daily_summary_output(text: str) -> str:
            bad_tail = re.compile(
                r"(?:至|为|升至|跌至|占比|估值|募资|成为除|补选|批评|取消|涨|跌)[\dA-Za-z%.\-]*[。.]$"
            )
            cleaned = []
            for line in (text or "").splitlines():
                m = re.match(r"^(\s*\d+\.\s+)(.+)$", line)
                if m and bad_tail.search(m.group(2).strip()):
                    continue
                cleaned.append(line)
            return "\n".join(cleaned).strip()

        def is_content_risk_error(exc: Exception) -> bool:
            msg = str(exc).lower()
            return (
                "content exists risk" in msg
                or "content risk" in msg
                or ("risk" in msg and "400" in msg)
            )

        def parse_selected_ids(raw: str) -> list[int]:
            match = re.search(r"\{.*\}", raw or "", flags=re.S)
            if not match:
                return []
            try:
                data = json.loads(match.group(0))
            except Exception:
                return []
            values = data.get("selected_ids") or data.get("ids") or []
            selected_ids = []
            for value in values:
                try:
                    selected_ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            return selected_ids

        excerpts = []
        seen_titles = set()
        for a in articles:
            art_id = a.get("id", 0)
            source = plain_text(a.get("source", "") or "?")
            title = compact_text(a.get("title", "") or "", max_title_chars)
            if is_invalid_digest_text(title, source):
                continue
            title_key = normalize_title_key(title)
            if title_key and title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            raw_summary = a.get("summary", "") or ""
            has_valid_summary = not is_invalid_digest_text(raw_summary, source)
            text = compact_text(raw_summary, max_summary_chars) if has_valid_summary else title
            if is_invalid_digest_text(text, source):
                continue
            category = classify_article(title, text, source)
            score, keyword_count = score_article(title, text, source, has_valid_summary)
            keyword_hits = [
                keyword for keyword in importance_keywords
                if keyword.lower() in f"{title} {text} {source}".lower()
            ][:8]
            excerpts.append({
                "id": art_id,
                "source": source,
                "title": title,
                "url": article_link(a),
                "text": text,
                "category": category,
                "has_summary": has_valid_summary,
                "score": score,
                "keyword_count": keyword_count,
                "keyword_hits": keyword_hits,
            })

        by_source = defaultdict(list)
        for ex in excerpts:
            by_source[ex["source"]].append(ex)

        capped = []
        for items in by_source.values():
            items.sort(key=lambda e: e.get("score", 0), reverse=True)
            capped.extend(items[:max_articles_per_source])
        articles_after_source_cap = len(capped)
        capped.sort(key=lambda e: e.get("score", 0), reverse=True)

        # Reserve each category's top-scored items before the global cut.
        # A pure global sort can wipe out an entire category (e.g. one made
        # up mostly of short, freshly-published items) before the AI
        # selection stage ever sees it; reserving a floor per category keeps
        # it in the running on merit within its own category.
        reserved_ids = set()
        reserved = []
        for category in categories:
            for item in capped:
                if item["category"] != category or item["id"] in reserved_ids:
                    continue
                reserved.append(item)
                reserved_ids.add(item["id"])
                if sum(1 for e in reserved if e["category"] == category) >= min_candidates_per_category:
                    break
        rest = [e for e in capped if e["id"] not in reserved_ids]
        remaining_slots = max(0, max_daily_candidates - len(reserved))
        capped = reserved + rest[:remaining_slots]
        capped.sort(key=lambda e: e.get("score", 0), reverse=True)
        capped = capped[:max_daily_candidates]
        articles_selected_for_ai = len(capped)

        if not capped:
            return {"summary": "今日无高质量新闻可总结。", "stats": {
                "total_articles": len(articles),
                "articles_after_dedup": len(articles),
                "articles_after_source_cap": 0,
                "articles_selected_for_ai": 0,
                "total_batches": 0,
                "articles_with_summary": 0,
                "articles_without_summary": 0,
                "selected_articles_with_summary": 0,
                "selected_articles_without_summary": 0,
                "daily_target_items": target_items,
                "selection_ai_used": False,
            }}

        fallback_reason = ""
        selection_ai_used = False
        selected_for_final = select_local(capped)
        selection_text = "\n\n".join(format_article_entry(e, i + 1) for i, e in enumerate(capped))
        selection_prompt = [
            {
                "role": "system",
                "content": (
                    "你是一个资深新闻选题编辑。请基于来源分类、关键词命中和内容事实，从候选文章中选择最值得进入每日摘要的文章。"
                    f"目标选择 {target_items} 条，允许 {min_items}-{max_items} 条。"
                    "请修正明显错误分类，合并重复事件，只保留信息量最高的一篇。"
                    "忽略 via-only、提示补充正文、缺少正文、无法摘要等低质量候选。"
                    "只输出 JSON，格式为 {\"selected_ids\":[1,2,3]}。"
                ),
            },
            {"role": "user", "content": f"以下是今日新闻候选列表：\n\n{selection_text}"},
        ]
        try:
            raw_selection = self.chat(
                selection_prompt,
                max_tokens=self._provider_output_cap(1200, "daily_continue"),
                temperature=0.1,
            )
            selected_ids = parse_selected_ids(raw_selection)
            id_map = {int(e["id"]): e for e in capped if e.get("id")}
            selected = []
            seen_ids = set()
            for selected_id in selected_ids:
                if selected_id in id_map and selected_id not in seen_ids:
                    selected.append(id_map[selected_id])
                    seen_ids.add(selected_id)
                if len(selected) >= max_items:
                    break
            if selected:
                for item in capped:
                    if len(selected) >= min_items:
                        break
                    if item.get("id") not in seen_ids:
                        selected.append(item)
                        seen_ids.add(item.get("id"))
                selected_for_final = selected[:max_items]
                selection_ai_used = True
        except Exception as e:
            fallback_reason = f"selection failed: {e}"

        total_articles_with_summary = sum(1 for e in capped if e["has_summary"])
        total_articles_without_summary = len(capped) - total_articles_with_summary
        selected_articles_with_summary = sum(1 for e in selected_for_final if e["has_summary"])
        selected_articles_without_summary = len(selected_for_final) - selected_articles_with_summary

        final_format_rules = (
            "请按以下四个大分类输出，分类标题只能使用这些名称：\n"
            "## 政经新闻\n"
            "## 科技动态\n"
            "## 商业聚焦\n"
            "## 其他信息\n\n"
            f"总条数目标为 {target_items} 条，允许 {min_items}-{max_items} 条；不要按分类平均凑数。\n"
            "根据文章重要性动态分配分类条数，素材不足的分类可以少写或不写，不得输出“（无相关新闻）”。\n"
            "每条必须使用如下格式：\n"
            "1. **总结性短标题：** 一句完整摘要 [🔗](URL)\n"
            "每个分类下必须使用有序编号列表，不要使用 '-' 或 '*' 作为项目符号。\n"
            "总结性短标题必须根据该条新闻内容生成，不要使用固定文案“单条摘要总结”。\n"
            "短标题加摘要正文尽量控制在 90 个中文字符以内；链接不计入字数。\n"
            "每条摘要必须是完整句子，不能以省略号结尾，不能使用“…”或“...”截断内容。\n"
            "不要输出“请补充正文”“无法生成摘要”“缺少文章全文”等模型说明或提示词残留。\n"
            "如果输入信息不足以写完整数字、机构名、地点或事件结果，就省略该细节，不要输出半截事实。\n"
            "每条末尾必须附文章链接，格式为 [🔗](URL)，且 URL 必须使用输入中的 https://news.rayyu.me/#/article/xxx 链接。\n"
            "不要输出总述、寒暄或额外说明；不要把链接集中放到末尾。"
        )

        articles_text = "\n\n".join(
            format_article_entry(e, i + 1)
            for i, e in enumerate(selected_for_final)
        )
        sys_msg = (
            "你是一个资深新闻编辑。请基于输入中的文章摘要或标题线索生成每日摘要。"
            "没有内容摘要的文章，只能根据标题线索做保守概括，不要编造标题之外的事实。"
            "允许根据内容事实对建议分类做最终纠偏。\n"
            + final_format_rules
        )
        final_prompt = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": f"以下是已经筛选出的今日重要新闻，请生成每日摘要：\n\n{articles_text}"},
        ]

        try:
            final_summary = self.chat(
                final_prompt,
                max_tokens=self._provider_output_cap(4200, "daily_final"),
            )
            if summary_looks_truncated(final_summary):
                continuation_prompt = final_prompt + [
                    {"role": "assistant", "content": final_summary},
                    {"role": "user", "content": "你的输出被截断了。请只从最后一条未完成的位置继续输出，保持相同 Markdown 格式，不要重复已经完成的条目。"},
                ]
                try:
                    final_summary = final_summary.rstrip() + "\n" + self.chat(
                        continuation_prompt,
                        max_tokens=self._provider_output_cap(1400, "daily_continue"),
                    )
                except Exception as e:
                    if not is_content_risk_error(e):
                        raise
                    fallback_reason = str(e)
        except Exception as e:
            if not is_content_risk_error(e):
                raise
            fallback_reason = str(e)
            final_summary = fallback_daily_summary(selected_for_final)
        final_summary = cleanup_daily_summary_output(final_summary)
        if "[🔗](" not in final_summary:
            final_summary = fallback_daily_summary(selected_for_final)
            fallback_reason = fallback_reason or "AI output missing links"
        elif summary_looks_truncated(final_summary):
            final_summary = fallback_daily_summary(selected_for_final)
            fallback_reason = fallback_reason or "AI output looked truncated"

        return {
            "summary": final_summary,
            "stats": {
                "total_articles": len(articles),
                "articles_after_dedup": len(articles),
                "articles_after_source_cap": articles_after_source_cap,
                "articles_selected_for_ai": articles_selected_for_ai,
                "total_batches": 2 if selection_ai_used else 1,
                "articles_with_summary": total_articles_with_summary,
                "articles_without_summary": total_articles_without_summary,
                "selected_articles_with_summary": selected_articles_with_summary,
                "selected_articles_without_summary": selected_articles_without_summary,
                "daily_target_items": target_items,
                "daily_min_items": min_items,
                "daily_max_items": max_items,
                "selection_ai_used": selection_ai_used,
                "fallback_reason": fallback_reason,
            },
        }

    # ─── Full HTML translation ────────────────────────────────

    def translate_full(self, html: str, target_lang: str = "zh-CN",
                       title: str = "") -> dict:
        """Translate full article HTML (and optionally title), preserving all tags.

        Returns {"title": translated_title, "html": translated_html}.
        If title is empty, translated_title will also be empty.
        """
        lang_name = "中文" if "zh" in target_lang else target_lang
        parts = []
        if title:
            parts.append(f"文章标题（请翻译并保持格式）：{title}")
        parts.append(f"文章正文 HTML（请翻译标签内文本，保持所有 HTML 结构不变）：\n{html}")
        user_content = "\n\n".join(parts)

        prompt = f"请将以下文章翻译为{lang_name}。\n\n"
        prompt += "要求：\n"
        if title:
            prompt += "0. 第一行输出翻译后的标题，不要任何标记或引号\n"
        prompt += "1. 只翻译标签内的文本内容，保持所有 HTML 标签不变\n"
        prompt += "2. 保持原文的段落、换行、超链接（<a>标签）、加粗（<b>）、斜体（<i>）等所有格式\n"
        prompt += "3. 不要添加、删除或修改任何 HTML 标签和属性\n"
        prompt += "4. 直接输出翻译后的 HTML，不要包含 ```html 代码块标记或任何额外说明文字\n\n"
        if title:
            prompt += "输出格式：\n第一行：翻译后的标题\n之后：翻译后的完整 HTML\n\n"

        messages = [
            {"role": "system",
             "content": f"你是一个专业的翻译助手。将文章翻译为{lang_name}，保持全部 HTML 标签和结构不变。"},
            {"role": "user",
             "content": prompt + user_content},
        ]
        result = self.chat(messages, max_tokens=8000, temperature=0.3)

        translated_title = ""
        translated_html = result
        if title:
            lines = result.split("\n", 1)
            translated_title = _normalize_cjk_quotes(lines[0].strip())
            translated_html = lines[1] if len(lines) > 1 else ""

        return {"title": translated_title, "html": translated_html}

    # ─── Batch translation ────────────────────────────────────

    def translate_batch(self, segments: list[dict]) -> list[dict]:
        """Translate multiple text segments at once.
        
        segments: [{"id": 0, "text": "..."}, {"id": 1, "text": "..."}, ...]
        Returns: [{"id": 0, "text": "译文..."}, {"id": 1, "text": "译文..."}, ...]
        
        Inline HTML tags (<b>, <i>, <a>) are preserved using markdown equivalents
        so the AI can keep them in the translation.
        """
        import re

        # Build numbered prompt
        lines = []
        for seg in segments:
            text = seg["text"]
            # Convert inline HTML to markdown markers for AI preservation
            text = text.replace("<b>", "**").replace("</b>", "**")
            text = text.replace("<strong>", "**").replace("</strong>", "**")
            text = text.replace("<i>", "*").replace("</i>", "*")
            text = text.replace("<em>", "*").replace("</em>", "*")
            # Convert <a href="url">text</a> → [text](url)
            text = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r'[\2](\1)', text)
            # Strip other tags (images, spans with no semantic value)
            text = re.sub(r'<[^>]+>', '', text)
            lines.append(f"[{seg['id']}] {text}")

        prompt = (
            "请将以下各段逐段翻译为中文。\n\n"
            "格式要求（非常重要）：\n"
            "1. 每段输出一行：原文编号 || 译文\n"
            "2. 保持原文中的 **加粗**、*斜体* 和 [链接文字](链接地址) 标记不变\n"
            "3. 不要添加任何额外说明文字\n\n"
            "示例：\n"
            "[0] This is **important** news. || 这是**重要**新闻。\n"
            "[1] Click [here](https://x.com) to visit. || 点击[这里](https://x.com)访问。\n\n"
            "待翻译段落：\n"
        )

        messages = [
            {"role": "system",
             "content": "你是一个专业的翻译助手。按编号逐段翻译，保留 **加粗** *斜体* 和 [链接文字](URL) 标记。"},
            {"role": "user",
             "content": prompt + "\n".join(lines)},
        ]
        result = self.chat(messages, max_tokens=4000)

        # Parse response: each line "[id] || translation"
        parsed = {}
        for line in result.split("\n"):
            line = line.strip()
            m = re.match(r'^\[(\d+)\]\s*\|\|\s*(.*)', line)
            if m:
                parsed[int(m.group(1))] = m.group(2).strip()

        # Build return array preserving original order
        output = []
        for seg in segments:
            trans = parsed.get(seg["id"], "")
            # Convert markdown markers back to HTML
            trans = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', trans)
            trans = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', trans)
            trans = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', trans)
            output.append({"id": seg["id"], "text": trans})
        return output

    # ─── Test connection ──────────────────────────────────────

    def test_connection(self) -> str:
        """Verify API key, endpoint, and the configured model with a tiny chat call."""
        old_timeout = self.request_timeout
        self.request_timeout = min(old_timeout, int(os.environ.get("AI_TEST_TIMEOUT_SECONDS", "20")))
        try:
            result = self.chat(
                [
                    {"role": "system", "content": "Reply with pong only."},
                    {"role": "user", "content": "ping"},
                ],
                max_tokens=50,
                temperature=0,
            )
        finally:
            self.request_timeout = old_timeout
        if not (result or "").strip():
            raise RuntimeError("AI API returned an empty chat completion")
        return f"连接成功，当前模型可用：{self.model}"
