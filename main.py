#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深海之声 · 重置版。

一条路，三个服务，插件只做调度：

    中文回复 ──► 翻译服务(8891) ──► 日语 ──► 语音服务(8890) ──► 语音条
                                          └► 消息链：[语音][中文原文]

设计上只保留必要的东西：
  · 两个服务由插件拉起，AstrBot 重启自动恢复
  · 翻译失败就不配音，只发文字，绝不阻断消息
  · 插件不加载任何模型，运存全留给服务
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import hmac
import hashlib

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

KANA = re.compile(r"[\u3040-\u30ff]")
QUOTE_LINE = (" > ", "```")
SPEAK_FIX = (("本鲸", "私"),)

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(PLUGIN_DIR, "..", ".."))
TRANSLATE_DIR = os.path.join(DATA_DIR, "translate")
TTS_DIR = os.path.join(DATA_DIR, "tts_service")


def has_kana(text: str) -> bool:
    return bool(KANA.search(text or ""))


def strip_noise(text: str) -> str:
    lines = [ln for ln in (text or "").split("\n") if not ln.lstrip().startswith(QUOTE_LINE)]
    return "\n".join(lines).strip()


class DeepseaVoice(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.cache_dir = os.path.join(PLUGIN_DIR, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        # 服务跟着插件活：重启 AstrBot，两个服务自己回来
        for fn in (self._ensure_tts,):
            try:
                import threading
                threading.Thread(target=fn, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 配置 ----------------
    def _cfg(self, key, default=None):
        value = self.config.get(key)
        return default if value in (None, "") else value

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            return False

    # ---------------- 服务生命周期 ----------------
    def _ensure_translate(self) -> None:
        if self._port_open(8891):
            return
        entry = os.path.join(TRANSLATE_DIR, "server.mjs")
        node = shutil.which("node")
        if not (os.path.exists(entry) and node):
            logger.warning("[深海之声] 翻译服务缺 node 或 server.mjs")
            return
        env = dict(os.environ, TRANSLATE_HOST="127.0.0.1", TRANSLATE_PORT="8891")
        try:
            subprocess.Popen([node, "--max-old-space-size=1536", entry],
                             cwd=TRANSLATE_DIR, env=env,
                             stdout=open("/tmp/translate.log", "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
            logger.info("[深海之声] 翻译服务已拉起")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[深海之声] 翻译服务启动失败：{exc}")

    def _ensure_tts(self) -> None:
        if self._port_open(8890):
            return
        entry = os.path.join(TTS_DIR, "server.py")
        if not os.path.exists(entry):
            logger.warning("[深海之声] 语音服务缺 server.py")
            return
        env = dict(os.environ, TTS_HOST="127.0.0.1",
                   VOICEVOX_DIR=str(self._cfg("voicevox_dir", "/root/AstrBot/data/voicevox")),
                   VOICEVOX_MODEL=str(self._cfg("voicevox_model", "0.vvm")),
                   VOICEVOX_STYLE=str(self._cfg("voicevox_style", 3)),
                   TTS_GAP="0.04")
        try:
            subprocess.Popen([sys.executable, entry], cwd=TTS_DIR, env=env,
                             stdout=open("/tmp/tts_service.log", "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
            logger.info("[深海之声] 语音服务已拉起")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[深海之声] 语音服务启动失败：{exc}")

    # ---------------- 调服务 ----------------
    def _post(self, url: str, payload: dict, timeout: float, raw: bool = False):
        import urllib.request
        body = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        return data if raw else json.loads(data.decode())

    def _translate(self, text: str) -> str:
        for a, b in SPEAK_FIX:
            text = text.replace(a, b)
        try:
            data = self._post(str(self._cfg("translate_url", "http://127.0.0.1:8891/translate")),
                              {"text": text[:400]},
                              float(self._cfg("translate_timeout", 90)))
            return str(data.get("ja") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[深海之声] 翻译失败：{exc}")
            return ""

    def _tts(self, text: str) -> str:
        try:
            audio = self._post(str(self._cfg("tts_url", "http://127.0.0.1:8890/tts")),
                               {"text": text[:600]},
                               float(self._cfg("tts_timeout", 120)), raw=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[深海之声] 合成失败：{exc}")
            return ""
        if not audio:
            return ""
        name = hashlib.sha1(f"{text}".encode()).hexdigest()[:16] + ".mp3"
        path = os.path.join(self.cache_dir, name)
        with open(path, "wb") as fh:
            fh.write(audio)
        os.chmod(path, 0o644)
        return path

    # ---------------- 钩子 ----------------
    @filter.on_decorating_result()
    async def decorate(self, event: AstrMessageEvent):
        if not bool(self._cfg("enable", True)):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result else None
        if not isinstance(chain, list) or not chain:
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:  # noqa: BLE001
            return

        plain = next((c for c in chain if isinstance(c, Plain) and c.text), None)
        if plain is None:
            return
        original = strip_noise(plain.text)
        if not original:
            return

        # 回复写成「日语 + 中文翻译」：日语那句念出来，文字只留中文
        if not has_kana(original):
            return
        segs = re.split(r"(?<=[。！？!?\n])", original)
        jp_text = "".join(x for x in segs if has_kana(x)).strip()
        zh_text = "".join(x for x in segs if x.strip() and not has_kana(x)).strip()
        if not jp_text:
            return
        voice_text = jp_text
        display = zh_text or original

        path = await asyncio.to_thread(self._tts, voice_text)
        if not path:
            return

        plain.text = display            # 文字保留中文（或日语原文）
        chain.insert(0, Record(file=path))
        result.chain = chain
