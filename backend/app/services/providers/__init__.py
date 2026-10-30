import asyncio
import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.core.config import AppError


@dataclass
class ModelConfig:
    id: str
    provider: str
    model: str
    base_url: str = ''
    api_key: str = field(default='', repr=False)
    temperature: float = 0.7
    max_tokens: int = 2048
    json_object: bool = False


@dataclass
class GenerationMetadata:
    model: str | None = None
    finish_reason: str | None = None
    usage: dict = field(default_factory=lambda: {'source': 'unknown', 'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None})

    def read_usage(self, value):
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError('Invalid usage')
        keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        if not all(type(value.get(k)) is int and value[k] >= 0 for k in keys):
            raise ValueError('Invalid usage counts')
        self.usage = {'source': 'api', **{k: value[k] for k in keys}}
        for k in ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
            if type(value.get(k)) is int and value[k] >= 0:
                self.usage[k] = value[k]


class ModelProvider(ABC):
    def __init__(self, config: ModelConfig):
        self.config = config

    async def chat(self, messages: list[dict]) -> str:
        return ''.join([part async for part in self.stream_chat(messages)])

    @abstractmethod
    async def stream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        yield ''

    def estimate_tokens(self, messages: list[dict]) -> int:
        # Conservative local estimate; never reported as actual API usage.
        return 32 + sum(len(m['content'].encode('utf-8')) + 32 for m in messages)

    async def stream_response(self, messages, metadata: GenerationMetadata):
        from contextlib import aclosing
        async with aclosing(self.stream_chat(messages)) as stream:
            async for part in stream:
                yield part
        metadata.model = self.config.model
        metadata.finish_reason = 'stop'


class MockProvider(ModelProvider):
    async def stream_chat(self, messages):
        prompt = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
        answer = f'【离线 Mock · {self.config.model}】\n\n已收到：{prompt}\n\n这是用于验证存储与流式链路的固定模拟回复，不是真实模型推理。'
        for i in range(0, len(answer), 8):
            await asyncio.sleep(0.015)
            yield answer[i:i + 8]


def response_error(status: int, body: str) -> AppError:
    # Inspect upstream errors only for classification, never return arbitrary bodies.
    lower = body.lower()
    if status in (401, 403):
        return AppError('authentication_error', '模型认证失败，请检查后端 API Key 和权限。', 502)
    if status == 429:
        return AppError('rate_limit', '模型请求受限或 API 余额不足，请检查供应商账户后重试。', 429)
    if status == 413 or any(x in lower for x in ('context_length', 'context window', 'maximum context', 'too many tokens')):
        return AppError('context_length', '上下文超过模型限制，请新建会话或调整上下文预算。', 422)
    if status == 404 or 'model_not_found' in lower:
        return AppError('model_not_found', '模型或接口路径不存在，请检查模型名与 Base URL。', 502)
    if status >= 500:
        return AppError('service_unavailable', '模型服务暂时不可用，请稍后重试。', 503)
    return AppError('provider_error', f'模型服务拒绝请求（HTTP {status}），请检查后端配置。', 502)


class OpenAICompatibleProvider(ModelProvider):
    async def stream_chat(self, messages):
        from contextlib import aclosing
        async with aclosing(self.stream_response(messages, GenerationMetadata())) as stream:
            async for part in stream:
                yield part

    async def stream_response(self, messages, metadata):
        headers = {'Authorization': f'Bearer {self.config.api_key}'} if self.config.api_key else {}
        payload = {'model': self.config.model, 'messages': messages, 'temperature': self.config.temperature, 'max_tokens': self.config.max_tokens, 'stream': True}
        if self.config.provider == 'deepseek':
            payload.update(thinking={'type': 'disabled'}, stream_options={'include_usage': True})
        if self.config.json_object:
            payload['response_format']={'type':'json_object'}
        finished = False
        done = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), trust_env=False, follow_redirects=False) as client:
                async with client.stream('POST', self.config.base_url.rstrip('/') + '/chat/completions', headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        body = (await response.aread()).decode('utf-8', errors='replace')
                        raise response_error(response.status_code, body)
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            done = True
                            break
                        try:
                            event = json.loads(data)
                            if not isinstance(event, dict):
                                raise ValueError('Expected object')
                            if 'error' in event:
                                raise response_error(400, json.dumps(event['error']))
                            if isinstance(event.get('model'), str):
                                metadata.model = event['model']
                            metadata.read_usage(event.get('usage'))
                            choices = event.get('choices', [])
                            if not isinstance(choices, list):
                                raise ValueError('Expected choices array')
                            if choices:
                                choice = choices[0]
                                if not isinstance(choice, dict) or not isinstance(choice.get('delta', {}), dict):
                                    raise ValueError('Expected choice and delta objects')
                                if choice.get('finish_reason') is not None:
                                    metadata.finish_reason = choice['finish_reason']
                                    if choice['finish_reason'] != 'stop':
                                        raise AppError('incomplete_response', '模型输出被截断或过滤，未作为完整回复保存；请调整输出上限后重试。', 502)
                                    finished = True
                                content = choice.get('delta', {}).get('content')
                                if content:
                                    if not isinstance(content, str):
                                        raise ValueError('Invalid content')
                                    yield content
                        except (ValueError, TypeError, KeyError) as exc:
                            raise AppError('provider_error', '模型返回了无法解析的流式数据。', 502) from exc
            if not finished or not done:
                raise AppError('incomplete_response', '模型连接提前结束，未保存不完整回复。', 502)
        except httpx.TimeoutException as exc:
            raise AppError('network_error', '模型连接超时；用户消息已保留。', 504) from exc
        except httpx.RequestError as exc:
            raise AppError('network_error', '无法连接模型服务，请检查网络和 Base URL。', 502) from exc


class ProviderRegistry:
    def __init__(self):
        self.providers: dict[str, ModelProvider] = {}
        for id, model in [('mock', 'mock-v1'), ('mock-alt', 'mock-v2')]:
            self.providers[id] = MockProvider(ModelConfig(id, 'mock', model))
        key = os.getenv('OPENAI_API_KEY', '')
        if key:
            self.providers['openai'] = OpenAICompatibleProvider(ModelConfig(
                'openai', 'openai-compatible', os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
                os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1'), key,
                float(os.getenv('OPENAI_TEMPERATURE', '0.7')), int(os.getenv('OPENAI_MAX_TOKENS', '2048'))))
        key = os.getenv('DEEPSEEK_API_KEY', '')
        if key:
            self.providers['deepseek'] = OpenAICompatibleProvider(ModelConfig(
                'deepseek', 'deepseek', os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'),
                os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'), key,
                float(os.getenv('DEEPSEEK_TEMPERATURE', '0.7')), int(os.getenv('DEEPSEEK_MAX_TOKENS', '2048'))))
        if os.getenv('LOCAL_MODEL'):
            self.providers['local'] = OpenAICompatibleProvider(ModelConfig('local', 'openai-compatible', os.environ['LOCAL_MODEL'], os.getenv('LOCAL_BASE_URL', 'http://127.0.0.1:11434/v1'), os.getenv('LOCAL_API_KEY', '')))

    def get(self, id: str) -> ModelProvider:
        if id not in self.providers:
            raise AppError('configuration_error', '所选模型配置不存在或尚未配置密钥。', 422)
        return self.providers[id]

    def public_configs(self) -> list[dict]:
        return [{'id': id, 'provider': p.config.provider, 'model': p.config.model} for id, p in self.providers.items()]
