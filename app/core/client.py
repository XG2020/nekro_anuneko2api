import json
import httpx
import asyncio
import hashlib
from typing import List
from app.core.config import settings
from app.models.protocol import Message

class AnunekoClient:
    def __init__(self):
        self.headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://anuneko.com",
            "referer": "https://anuneko.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "x-app_id": "com.anuttacon.neko",
            "x-client_type": "4",
            "x-token": settings.ANUNEKO_TOKEN,
        }
        if settings.ANUNEKO_COOKIE:
            self.headers["Cookie"] = settings.ANUNEKO_COOKIE

        # 缓存：对话指纹 -> Chat ID
        # 指纹通常由对话的第一条消息决定
        self.fingerprint_map = {} 
        
        # 缓存：Chat ID -> 当前模型
        self.chat_model_map = {}

    def _get_api_model_name(self, input_model: str) -> str:
        """映射 OpenAI 模型名到 Anuneko 模型名"""
        input_model = input_model.lower()
        if "black" in input_model or "exotic" in input_model:
            return "Exotic Shorthair" # 黑猫
        return "Orange Cat" # 默认橘猫

    def _extract_chat_key(self, content: str) -> str:
        """
        从消息内容中提取Current Chat Key
        格式通常为：onebot_v11-group_685782298、onebot_v11-private_2139954865、tg-private_545646485等
        """
        # 查找Current Chat Key: 后面的内容
        import re
        match = re.search(r'Current Chat Key: ([\w\-]+)', content)
        if match:
            return match.group(1)
        return "default_key"
    
    def _clean_message_content(self, content: str) -> str:
        """
        去除消息开头的特定字段
        """
        # 使用正则表达式匹配并移除开头的特定字段
        import re
        # 匹配从开头到'Recent Messages:'（包括）的所有内容
        pattern = r'^Continue\. Next is a real user conversation scene\. Note that the sandbox before this has been cleaned up, and do not use the previously generated resources\. Current Chat Key: [\w\-]+ Current Time: [^|]+ \| 农历: [^\n]+ Recent Messages: '  
        cleaned_content = re.sub(pattern, '', content, count=1)
        
        # 如果正则没有匹配到，尝试更简单的方式
        if cleaned_content == content:
            # 尝试找到'Recent Messages:'后截取
            recent_pos = content.find('Recent Messages:')
            if recent_pos > 0:
                return content[recent_pos:].strip()
        
        return cleaned_content
    
    def _generate_fingerprint(self, messages: List[Message]) -> str:
        """
        生成对话指纹。
        策略：从消息中提取Current Chat Key作为指纹基准。
        这样相同的Current Chat Key会对应到同一个session。
        """
        if not messages:
            return "empty"
        
        # 从最后一条用户消息中提取Chat Key作为指纹基准
        last_user_msg = None
        for msg in reversed(messages):
            if msg.role == "user":
                last_user_msg = msg
                break
        
        if last_user_msg:
            chat_key = self._extract_chat_key(last_user_msg.content)
            return chat_key
        
        # 如果找不到用户消息，使用第一条消息内容作为备选方案
        base_content = messages[0].content
        return hashlib.md5(base_content.encode('utf-8')).hexdigest()

    async def get_session_and_prompt(self, messages: List[Message], model_input: str):
        """
        根据传入的消息历史，决定是新建会话还是复用会话。
        返回: (chat_id, prompt_to_send)
        """
        target_model = self._get_api_model_name(model_input)
        fingerprint = self._generate_fingerprint(messages)
        
        # 提取最后一条用户消息作为发送内容，并清理消息内容
        last_msg = messages[-1]
        raw_prompt = last_msg.content if last_msg.role == "user" else "..."
        prompt = self._clean_message_content(raw_prompt)

        # 优先从缓存中查找会话，不再根据消息长度判断是否为新对话
        chat_id = self.fingerprint_map.get(fingerprint)

        # 如果没有找到现有会话，则创建新会话
        if not chat_id:
            chat_id = await self._create_session(target_model)
            if chat_id:
                # 更新指纹映射
                self.fingerprint_map[fingerprint] = chat_id
                self.chat_model_map[chat_id] = target_model
            else:
                raise Exception("Failed to create upstream session")
        
        # 检查是否需要切换模型 (针对复用的会话)
        current_model = self.chat_model_map.get(chat_id)
        if current_model != target_model:
            await self._switch_model(chat_id, target_model)
            self.chat_model_map[chat_id] = target_model
            
        return chat_id, prompt

    async def _create_session(self, model_name: str) -> str:
        url = f"{settings.BASE_URL}/chat"
        data = {"model": model_name}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, headers=self.headers, json=data)
                rj = resp.json()
                cid = rj.get("chat_id") or rj.get("id")
                # 创建时顺便确认一下模型，保证一致性
                if cid:
                     asyncio.create_task(self._switch_model(cid, model_name))
                return cid
        except Exception as e:
            print(f"Create session error: {e}")
            return None

    async def _switch_model(self, chat_id: str, model_name: str):
        url = f"{settings.BASE_URL}/user/select_model"
        data = {"chat_id": chat_id, "model": model_name}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(url, headers=self.headers, json=data)
        except:
            pass

    async def _send_choice(self, msg_id: str):
        url = f"{settings.BASE_URL}/msg/select-choice"
        data = {"msg_id": msg_id, "choice_idx": 0}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(url, headers=self.headers, json=data)
        except:
            pass

    async def chat_stream(self, chat_id: str, text: str):
        """生成器：返回文本片段"""
        url = f"{settings.BASE_URL}/msg/{chat_id}/stream"
        data = json.dumps({"contents": [text]}, ensure_ascii=False)
        
        headers = self.headers.copy()
        headers["Content-Type"] = "text/plain" 
        
        current_msg_id = None
        
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", url, headers=headers, content=data) as resp:
                async for line in resp.aiter_lines():
                    if not line: continue
                    # 兼容非 SSE 格式的错误返回
                    if not line.startswith("data: "): 
                        continue
                    
                    try:
                        raw = line[6:]
                        if not raw.strip(): continue
                        j = json.loads(raw)
                        
                        if "msg_id" in j:
                            current_msg_id = j["msg_id"]
                        
                        content = ""
                        # 解析复杂的分支结构
                        if "c" in j and isinstance(j["c"], list):
                            for choice in j["c"]:
                                if choice.get("c", 0) == 0 and "v" in choice:
                                    content += choice["v"]
                        elif "v" in j and isinstance(j["v"], str):
                            content += j["v"]
                            
                        if content:
                            yield content
                            
                    except Exception:
                        continue
        
        # 流结束后自动选择第一个分支
        if current_msg_id:
            asyncio.create_task(self._send_choice(current_msg_id))

anuneko_client = AnunekoClient()
