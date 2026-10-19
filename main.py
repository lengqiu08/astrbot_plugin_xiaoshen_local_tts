#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深海之声·客户端：只有日语才配音，语音交给本地 TTS 服务做。

自己做两件事：
  1. 判断这条回复是不是日语（有假名才算）
  2. 把文本 POST 给 http://127.0.0.1:8890/tts，拿回 mp3，挂成语音条

合成、模型、音色全在服务那边，插件这边不加载任何模型——所以它不会吃内存，
也不可能有"缺模型"这类报错。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

_KANA = re.compile(r"[\u3040-\u30ff]")


def is_japanese(text: str) -> bool:
    return bool(_KANA.search(text or ""))


def split_chunks(text: str, limit: int = 200) -> list[str]:
    """按句号切块，每块不超过 limit 字——QQ 一条语音最多 60 秒，长回复得拆开发。"""
    sents = re.split(r"(?<=[。！？.!?])", text)
    chunks: list[str] = []
    cur = ""
    for sent in sents:
        if not sent:
            continue
        if cur and len(cur) + len(sent) > limit:
            chunks.append(cur)
            cur = sent
        else:
            cur += sent
    if cur:
        chunks.append(cur)
    # 最后一块太短就并回前一块，免得甩出一条一两秒的碎语音
    if len(chunks) > 1 and len(chunks[-1]) < 50:
        chunks[-2] += chunks[-1]
        chunks.pop()
    return chunks


class DeepseaVoiceClient(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(self.plugin_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def _cfg(self, key, default):
        value = self.config.get(key, default)
        return default if value in (None, "") else value

    def _ask_service(self, text: str) -> str:
        """调本地服务，返回 mp3 路径；失败返回空串。"""
        url = str(self._cfg("service_url", "http://127.0.0.1:8890/tts"))
        payload = json.dumps({"text": text}, ensure_ascii=False).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=float(self._cfg("timeout", 60))) as resp:
            audio = resp.read()
        if not audio:
            return ""
        out = os.path.join(self.cache_dir, f"{int(time.time() * 1000)}.mp3")
        with open(out, "wb") as fh:
            fh.write(audio)
        os.chmod(out, 0o644)
        self._clean_cache()
        return out

    def _clean_cache(self, keep: int = 60) -> None:
        try:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir)]
            files = [f for f in files if os.path.isfile(f)]
            if len(files) > keep:
                files.sort(key=os.path.getmtime)
                for path in files[: len(files) - keep]:
                    os.remove(path)
        except OSError:
            pass

    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent):
        if not bool(self._cfg("enable", True)):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result else None
        if not chain or not isinstance(chain, list):
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:  # noqa: BLE001
            return

        texts = [c.text for c in chain if isinstance(c, Plain) and c.text]
        if not texts:
            return
        raw = "".join(texts)
        # 中文也配音：交给日语引擎按汉字音读，出来就是"日式中文"
        if not is_japanese(raw) and not bool(self._cfg("chinese_too", True)):
            return

        limit = int(self._cfg("max_chars", 300) or 300)
        text = raw.strip()[: limit * 2]          # 先粗截，防止一条消息读五分钟
        pieces = split_chunks(text, min(limit, 200))
        import asyncio
        for piece in pieces:
            try:
                path = await asyncio.to_thread(self._ask_service, piece)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaVoiceClient] 调服务失败：{exc}")
                continue
            if path and os.path.exists(path):
                chain.append(Record(file=path, text=piece))
        if len(chain) > 1:
            result.chain = chain
