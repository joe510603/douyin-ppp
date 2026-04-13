"""LLM API 客户端 — 支持 OpenAI / DeepSeek 等 API"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import httpx

from ..config import get_config
from .logger import get_logger

log = get_logger("llm_client")


class LLMClient:
    """LLM API 客户端"""
    
    def __init__(self):
        self.config = get_config().llm
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self
    
    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()
    
    @property
    def is_enabled(self) -> bool:
        """LLM 是否启用"""
        return self.config.enabled and bool(self.config.api_key)
    
    @property
    def base_url(self) -> str:
        """API Base URL"""
        if self.config.base_url:
            return self.config.base_url.rstrip("/")
        
        # 默认 URL
        if self.config.provider == "deepseek":
            return "https://api.deepseek.com/v1"
        return "https://api.openai.com/v1"
    
    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> Optional[str]:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大返回 token 数
            
        Returns:
            助手回复文本，失败返回 None
        """
        if not self.is_enabled:
            log.warning("LLM 未启用或未配置 API Key")
            return None
        
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        # 重试逻辑
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await self._client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return content
                
            except httpx.HTTPStatusError as e:
                log.error(f"LLM API 错误 (attempt {attempt+1}/{max_retries}): {e.response.status_code} - {e.response.text}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # 指数退避
                else:
                    return None
                    
            except Exception as e:
                log.error(f"LLM 请求失败: {e}")
                return None
        
        return None
    
    async def analyze_batch(
        self,
        texts: list[str],
        task: str,
        batch_size: int = None,
    ) -> list[dict]:
        """
        批量分析文本
        
        Args:
            texts: 文本列表
            task: 任务类型 (sentiment/intent/competitor/profile/cluster)
            batch_size: 每批数量，默认使用配置
            
        Returns:
            分析结果列表
        """
        if batch_size is None:
            batch_size = self.config.batch_size
        
        results = []
        total_batches = (len(texts) + batch_size - 1) // batch_size
        
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_num = i // batch_size + 1
            
            log.info(f"正在处理批次 {batch_num}/{total_batches}（{len(batch)} 条）")
            
            # 根据任务类型调用不同的分析方法
            if task == "sentiment":
                batch_result = await self._analyze_sentiment_batch(batch)
            elif task == "intent":
                batch_result = await self._analyze_intent_batch(batch)
            elif task == "competitor":
                batch_result = await self._analyze_competitor_batch(batch)
            elif task == "profile":
                batch_result = await self._analyze_profile_batch(batch)
            elif task == "cluster":
                batch_result = await self._analyze_cluster_batch(batch)
            else:
                log.error(f"未知任务类型: {task}")
                break
            
            if batch_result:
                results.extend(batch_result)
            
            # 避免请求过快
            if batch_num < total_batches:
                await asyncio.sleep(1)
        
        return results
    
    async def _analyze_sentiment_batch(self, texts: list[str]) -> list[dict]:
        """情感分析批量处理"""
        prompt = f"""请分析以下评论的情感倾向，返回 JSON 数组，每个元素包含：
- text: 原文本（前30字符）
- sentiment: "positive"（正面）/"negative"（负面）/"neutral"（中性）

评论列表：
{json.dumps([t[:100] for t in texts], ensure_ascii=False, indent=2)}

仅返回 JSON 数组，不要其他内容。"""

        response = await self.chat([{"role": "user", "content": prompt}], temperature=0.3)
        
        if response:
            try:
                # 尝试解析 JSON
                response = response.strip()
                if response.startswith("```"):
                    response = response.split("```")[1]
                    if response.startswith("json"):
                        response = response[4:]
                return json.loads(response)
            except json.JSONDecodeError:
                log.error(f"情感分析结果解析失败: {response[:200]}")
        
        return []
    
    async def _analyze_intent_batch(self, texts: list[str]) -> list[dict]:
        """意图分类批量处理"""
        prompt = f"""请分析以下评论的意图，返回 JSON 数组，每个元素包含：
- text: 原文本（前30字符）
- intent: "purchase"（购买意向）/"experience"（体验反馈）/"question"（疑问）/"complaint"（吐槽）/"interaction"（互动）

评论列表：
{json.dumps([t[:100] for t in texts], ensure_ascii=False, indent=2)}

仅返回 JSON 数组。"""

        response = await self.chat([{"role": "user", "content": prompt}], temperature=0.3)
        
        if response:
            try:
                response = response.strip()
                if response.startswith("```"):
                    response = response.split("```")[1]
                    if response.startswith("json"):
                        response = response[4:]
                return json.loads(response)
            except json.JSONDecodeError:
                log.error(f"意图分类结果解析失败")
        
        return []
    
    async def _analyze_competitor_batch(self, texts: list[str]) -> list[dict]:
        """竞品对比批量处理"""
        prompt = f"""请从以下评论中提取竞品提及，返回 JSON 数组，每个元素包含：
- text: 原文本（前30字符）
- competitors: 提到的竞品品牌列表（如果没有则返回空数组）

评论列表：
{json.dumps([t[:100] for t in texts], ensure_ascii=False, indent=2)}

仅返回 JSON 数组。"""

        response = await self.chat([{"role": "user", "content": prompt}], temperature=0.3)
        
        if response:
            try:
                response = response.strip()
                if response.startswith("```"):
                    response = response.split("```")[1]
                    if response.startswith("json"):
                        response = response[4:]
                return json.loads(response)
            except json.JSONDecodeError:
                log.error(f"竞品分析结果解析失败")
        
        return []
    
    async def _analyze_profile_batch(self, texts: list[str]) -> list[dict]:
        """用户画像批量处理"""
        prompt = f"""请从以下评论中提取用户画像信息，返回 JSON 数组，每个元素包含：
- text: 原文本（前30字符）
- age: 年龄段（如"学生党"/"宝妈"/"打工人"，无法判断则返回null）
- skin_type: 肤质（如"油皮"/"干皮"/"敏感肌"，无法判断则返回null）
- location: 地区（如"北京"/"广东"，无法判断则返回null）

评论列表：
{json.dumps([t[:100] for t in texts], ensure_ascii=False, indent=2)}

仅返回 JSON 数组。"""

        response = await self.chat([{"role": "user", "content": prompt}], temperature=0.3)
        
        if response:
            try:
                response = response.strip()
                if response.startswith("```"):
                    response = response.split("```")[1]
                    if response.startswith("json"):
                        response = response[4:]
                return json.loads(response)
            except json.JSONDecodeError:
                log.error(f"用户画像分析结果解析失败")
        
        return []
    
    async def _analyze_cluster_batch(self, texts: list[str]) -> list[dict]:
        """热词聚类批量处理"""
        prompt = f"""请将以下评论按主题聚类，返回 JSON 对象，包含：
- clusters: 数组，每个元素包含 {{"theme": "主题名称", "keywords": ["关键词1", "关键词2"], "count": 评论数量}}

评论列表：
{json.dumps([t[:100] for t in texts], ensure_ascii=False, indent=2)}

仅返回 JSON 对象。"""

        response = await self.chat([{"role": "user", "content": prompt}], temperature=0.5)
        
        if response:
            try:
                response = response.strip()
                if response.startswith("```"):
                    response = response.split("```")[1]
                    if response.startswith("json"):
                        response = response[4:]
                return json.loads(response).get("clusters", [])
            except json.JSONDecodeError:
                log.error(f"热词聚类结果解析失败")
        
        return []


async def get_llm_client() -> LLMClient:
    """获取 LLM 客户端实例"""
    return LLMClient()