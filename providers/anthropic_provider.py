"""Anthropic 后端：走官方 SDK 的 Messages API，工具用 content-block 形式。
联网搜索用原生 server tool（web_search / web_fetch，服务端执行）。
adaptive thinking 默认开启；assistant 轮通过 raw 忠实回放以保留 thinking / server-tool 块。
"""
from __future__ import annotations

import base64
from typing import Optional

from .base import LLMProvider, Message, ToolCall, LLMResponse, sanitize_history
from image_prep import load_image_for_llm

# 支持 dynamic filtering 的新版 server tool（opus 4.6+ / sonnet 4.6+ / opus 5 / fable 5）
WEB_SEARCH_TOOL = {'type': 'web_search_20260209', 'name': 'web_search'}
WEB_FETCH_TOOL = {'type': 'web_fetch_20260209', 'name': 'web_fetch'}

# server 端工具在 assistant.raw 里的块类型；web search 关闭时这些块不可回放
_SERVER_BLOCK_TYPES = {'server_tool_use', 'web_search_tool_result', 'web_fetch_tool_result'}


def _raw_has_server_blocks(raw) -> bool:
    return isinstance(raw, list) and any(
        isinstance(b, dict) and b.get('type') in _SERVER_BLOCK_TYPES for b in raw
    )


def _block_to_dict(block) -> dict:
    """把 SDK 的 content block 转成可 JSON 持久化、也可原样回放的 dict。"""
    try:
        return block.model_dump(exclude_none=True)
    except Exception:
        d = {'type': getattr(block, 'type', 'text')}
        if hasattr(block, 'text'):
            d['text'] = block.text
        return d


def _image_block(path: str) -> Optional[dict]:
    loaded = load_image_for_llm(path)  # HEIC→jpeg、过大缩放，保证 mime 受支持
    if not loaded:
        return None
    data, mime = loaded
    b64 = base64.standard_b64encode(data).decode('ascii')
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': b64}}


class AnthropicProvider(LLMProvider):
    name = 'anthropic'

    def __init__(self, cfg: dict):
        import anthropic  # 延迟导入
        self.cfg = cfg
        self.model = (cfg.get('anthropic_model') or 'claude-opus-5').strip()
        api_key = (cfg.get('anthropic_api_key') or '').strip()
        if not api_key:
            raise ValueError("anthropic_api_key 未配置")
        base_url = (cfg.get('anthropic_base_url') or '').strip() or None
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        self.max_tokens = int(cfg.get('anthropic_max_tokens') or cfg.get('max_tokens') or 8192)
        self.enable_web_search = bool(cfg.get('enable_web_search'))
        self.use_thinking = cfg.get('anthropic_thinking', True)

    # ---- 请求组装 ----
    def _translate(self, messages: list[Message]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        api_messages: list[dict] = []
        pending_tool_results: list[dict] = []

        def flush_tools():
            if pending_tool_results:
                api_messages.append({'role': 'user', 'content': list(pending_tool_results)})
                pending_tool_results.clear()

        for m in messages:
            if m.role == 'tool':
                pending_tool_results.append({
                    'type': 'tool_result',
                    'tool_use_id': m.tool_call_id,
                    'content': m.content or '',
                })
                continue

            flush_tools()  # 遇到非 tool 消息前，先把攒着的 tool_result 作为一个 user 轮 flush

            if m.role == 'system':
                if m.content:
                    system_parts.append(m.content)
            elif m.role == 'user':
                if m.images:
                    blocks: list[dict] = []
                    for p in m.images:
                        b = _image_block(p)
                        if b:
                            blocks.append(b)
                    if m.content:
                        blocks.append({'type': 'text', 'text': m.content})
                    api_messages.append({'role': 'user', 'content': blocks or (m.content or '')})
                else:
                    api_messages.append({'role': 'user', 'content': m.content or ''})
            elif m.role == 'assistant':
                # 能忠实回放 raw 的前提：同 provider，且（web search 已关时）raw 里没有 server 工具块，
                # 否则请求不声明 server 工具却带着它的调用/结果块 → 400。不满足则用 generic 重建。
                raw_ok = (
                    m.raw is not None
                    and m.raw_provider == self.name
                    and not (not self.enable_web_search and _raw_has_server_blocks(m.raw))
                )
                if raw_ok:
                    api_messages.append({'role': 'assistant', 'content': m.raw})
                else:
                    blocks = []
                    if m.content:
                        blocks.append({'type': 'text', 'text': m.content})
                    for tc in m.tool_calls:
                        blocks.append({'type': 'tool_use', 'id': tc.id, 'name': tc.name, 'input': tc.arguments})
                    api_messages.append({'role': 'assistant', 'content': blocks or [{'type': 'text', 'text': ''}]})

        flush_tools()
        return '\n\n'.join(system_parts), api_messages

    def _tools_param(self, tools: Optional[list[dict]]) -> Optional[list[dict]]:
        out: list[dict] = []
        for t in (tools or []):
            out.append({
                'name': t['name'],
                'description': t.get('description', ''),
                'input_schema': t.get('parameters') or {'type': 'object', 'properties': {}},
            })
        if self.enable_web_search:
            out.append(WEB_SEARCH_TOOL)
            out.append(WEB_FETCH_TOOL)
        return out or None

    def chat(self, messages: list[Message], tools: Optional[list[dict]] = None) -> LLMResponse:
        allowed = {t['name'] for t in (tools or [])}
        messages = sanitize_history(messages, allowed)
        system_text, api_messages = self._translate(messages)
        tools_param = self._tools_param(tools)

        kwargs = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'messages': api_messages,
        }
        if system_text:
            kwargs['system'] = system_text
        if tools_param:
            kwargs['tools'] = tools_param
        if self.use_thinking:
            kwargs['thinking'] = {'type': 'adaptive'}

        # server 端工具可能触发 pause_turn，需要在同一轮里续跑。
        # 累积每个 paused 段的块，最终 raw 用「所有段拼接」，否则下一轮回放只剩最后一段，
        # 可能把 server_tool_use 与它配对的 result 拆散 → 孤立块被 API 拒绝。
        resp = self.client.messages.create(**kwargs)
        accumulated: list[dict] = []
        for _ in range(5):
            if getattr(resp, 'stop_reason', None) != 'pause_turn':
                break
            seg = [_block_to_dict(b) for b in resp.content]  # 转 dict，别把 SDK 对象塞进 dict 列表
            accumulated.extend(seg)
            api_messages.append({'role': 'assistant', 'content': seg})
            kwargs['messages'] = api_messages
            resp = self.client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        refusal = getattr(resp, 'stop_reason', None) == 'refusal'
        for block in resp.content:
            btype = getattr(block, 'type', None)
            if btype == 'text':
                text_parts.append(block.text)
            elif btype == 'tool_use':  # 客户端自定义工具（server_tool_use 不在此列）
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input or {}))

        text = ''.join(text_parts).strip()
        if refusal and not text:
            text = '（很抱歉，这个请求我没法处理。）'

        assistant_message = Message(
            role='assistant',
            content=text,
            tool_calls=tool_calls,
            raw=accumulated + [_block_to_dict(b) for b in resp.content],
            raw_provider=self.name,
        )
        usage = {}
        try:
            usage = {'input': resp.usage.input_tokens, 'output': resp.usage.output_tokens}
        except Exception:
            pass
        return LLMResponse(
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            text=text,
            finish_reason=getattr(resp, 'stop_reason', '') or '',
            usage=usage,
        )
