"""OpenAI 兼容后端：base_url 可配，指向 DeepSeek / Qwen / GLM / 本地 vLLM 等。
走标准 chat.completions + function calling。联网搜索用端点自带能力（extra_body 传参）。
"""
from __future__ import annotations

import base64
import json
from typing import Optional

from .base import LLMProvider, Message, ToolCall, LLMResponse, sanitize_history
from text_format import strip_think
from image_prep import load_image_for_llm


def _strip_think(text: str) -> str:
    return (strip_think(text) or '').strip()


def _image_data_url(path: str) -> Optional[str]:
    loaded = load_image_for_llm(path)  # HEIC→jpeg、过大缩放，保证 mime 受支持
    if not loaded:
        return None
    data, mime = loaded
    b64 = base64.b64encode(data).decode('ascii')
    return f"data:{mime};base64,{b64}"


class OpenAIProvider(LLMProvider):
    name = 'openai'

    def __init__(self, cfg: dict):
        from openai import OpenAI  # 延迟导入，未装依赖时不影响其它 provider
        self.cfg = cfg
        self.model = (cfg.get('openai_model') or '').strip()
        if not self.model:
            raise ValueError("openai_model 未配置")
        base_url = (cfg.get('openai_base_url') or '').strip() or None
        api_key = (cfg.get('openai_api_key') or '').strip() or 'not-needed'
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.enable_web_search = bool(cfg.get('enable_web_search'))
        # 联网搜索参数名，默认空 = 不注入。只有支持的端点（Qwen/DashScope 的 enable_search）才填，
        # 否则给 DeepSeek/OpenAI 等塞未知字段会 400。
        self.search_param = (cfg.get('openai_search_param') or '').strip()
        self.max_tokens = int(cfg.get('max_tokens') or 4096)

    # ---- 请求组装 ----
    def _to_openai_messages(self, messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == 'assistant' and m.raw is not None and m.raw_provider == self.name:
                out.append(m.raw)  # 忠实回放本 provider 产出的 assistant 轮
                continue
            if m.role == 'system':
                out.append({'role': 'system', 'content': m.content})
            elif m.role == 'user':
                if m.images:
                    parts: list[dict] = []
                    if m.content:
                        parts.append({'type': 'text', 'text': m.content})
                    for p in m.images:
                        url = _image_data_url(p)
                        if url:
                            parts.append({'type': 'image_url', 'image_url': {'url': url}})
                    out.append({'role': 'user', 'content': parts or (m.content or '')})
                else:
                    out.append({'role': 'user', 'content': m.content or ''})
            elif m.role == 'assistant':
                msg: dict = {'role': 'assistant', 'content': m.content or ''}
                if m.tool_calls:
                    msg['tool_calls'] = [{
                        'id': tc.id,
                        'type': 'function',
                        'function': {'name': tc.name, 'arguments': json.dumps(tc.arguments, ensure_ascii=False)},
                    } for tc in m.tool_calls]
                    if not m.content:
                        msg['content'] = None
                out.append(msg)
            elif m.role == 'tool':
                out.append({'role': 'tool', 'tool_call_id': m.tool_call_id, 'content': m.content or ''})
        return out

    @staticmethod
    def _to_openai_tools(tools: Optional[list[dict]]) -> Optional[list[dict]]:
        if not tools:
            return None
        return [{
            'type': 'function',
            'function': {
                'name': t['name'],
                'description': t.get('description', ''),
                'parameters': t.get('parameters') or {'type': 'object', 'properties': {}},
            },
        } for t in tools]

    def chat(self, messages: list[Message], tools: Optional[list[dict]] = None) -> LLMResponse:
        allowed = {t['name'] for t in (tools or [])}
        messages = sanitize_history(messages, allowed)
        kwargs = {
            'model': self.model,
            'messages': self._to_openai_messages(messages),
            'max_tokens': self.max_tokens,
        }
        oai_tools = self._to_openai_tools(tools)
        if oai_tools:
            kwargs['tools'] = oai_tools
        if self.enable_web_search and self.search_param:
            kwargs['extra_body'] = {self.search_param: True}

        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                args = {'_raw': tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        text = _strip_think(msg.content or '')
        try:
            raw = msg.model_dump()
        except Exception:
            raw = {'role': 'assistant', 'content': msg.content or ''}

        assistant_message = Message(
            role='assistant',
            content=text,
            tool_calls=tool_calls,
            raw=raw,
            raw_provider=self.name,
        )
        usage = {}
        try:
            if resp.usage:
                usage = {'input': resp.usage.prompt_tokens, 'output': resp.usage.completion_tokens}
        except Exception:
            pass
        return LLMResponse(
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            text=text,
            finish_reason=choice.finish_reason or '',
            usage=usage,
        )
