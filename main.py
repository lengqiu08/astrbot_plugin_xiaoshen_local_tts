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

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    import pilk          # silk 编码：QQ 语音条原生格式，比 mp3 更稳
except ImportError:  # pragma: no cover
    pilk = None

SILK_RATE = 24000

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
        # 翻译服务跟着插件走：进程不在就拉起来（线程里做，别卡住加载）
        try:
            import threading
            threading.Thread(target=self._ensure_translate_service, daemon=True).start()
            threading.Thread(target=self._ensure_tts_service, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    def _port_open(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def _ensure_tts_service(self) -> None:
        """VOICEVOX 语音服务不在就拉起来（Python 进程）。"""
        url = str(self._cfg("service_url", "http://127.0.0.1:8890/tts") or "")
        if "8890" not in url or self._port_open("127.0.0.1", 8890):
            return
        script = os.path.abspath(os.path.join(self.plugin_dir, "..", "..", "tts_service", "server.py"))
        if not os.path.exists(script):
            logger.warning(f"[DeepseaVoiceClient] 找不到语音服务：{script}")
            return
        env = dict(os.environ,
                   TTS_HOST="127.0.0.1",
                   VOICEVOX_DIR=str(self._cfg("voicevox_dir", "/root/AstrBot/data/voicevox")),
                   VOICEVOX_MODEL=str(self._cfg("voicevox_model", "0.vvm")),
                   VOICEVOX_STYLE=str(self._cfg("voicevox_style", 3)),
                   TTS_GAP="0.04")
        try:
            subprocess.Popen(
                [sys.executable, script],
                cwd=os.path.dirname(script), env=env,
                stdout=open("/tmp/tts_service.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True)
            logger.info("[DeepseaVoiceClient] 语音服务已拉起")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaVoiceClient] 语音服务启动失败：{exc}")

    def _ensure_translate_service(self) -> None:
        """翻译服务不在就拉起来——跟着 AstrBot 一起活，重启不会掉。"""
        url = str(self._cfg("translate_url", "http://127.0.0.1:8891/translate") or "")
        if "8891" not in url:
            return
        if self._port_open("127.0.0.1", 8891):
            return
        base = os.path.abspath(os.path.join(self.plugin_dir, "..", "..", "translate"))
        entry = os.path.join(base, "server.mjs")
        if not os.path.exists(entry):
            logger.warning(f"[DeepseaVoiceClient] 找不到翻译服务：{entry}")
            return
        node = shutil.which("node")
        if not node:
            logger.warning("[DeepseaVoiceClient] 没装 node，翻译服务起不来")
            return
        env = dict(os.environ, TRANSLATE_HOST="127.0.0.1", TRANSLATE_PORT="8891")
        try:
            subprocess.Popen(
                [node, "--max-old-space-size=1536", entry],
                cwd=base, env=env,
                stdout=open("/tmp/translate.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True)
            logger.info("[DeepseaVoiceClient] 翻译服务已拉起（后台加载模型）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaVoiceClient] 翻译服务启动失败：{exc}")

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

    @staticmethod
    def _to_silk(mp3: str) -> str:
        """转成 QQ 语音条的原生格式，参考点歌插件那套做法。"""
        if pilk is None:
            return mp3
        pcm = os.path.join(tempfile.gettempdir(), f"tts_{os.getpid()}_{os.urandom(3).hex()}.pcm")
        silk = pcm.replace(".pcm", ".silk")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
                 "-f", "s16le", "-ar", str(SILK_RATE), "-ac", "1", pcm],
                check=True, timeout=300)
            if not pilk.encode(pcm, silk, SILK_RATE):
                return mp3
            return silk
        except Exception:  # noqa: BLE001
            return mp3
        finally:
            try:
                os.remove(pcm)
            except OSError:
                pass

    # 说话习惯：翻译前先把自称换成日语的第一人称，免得"本鲸"被译成兔子
    _SPEAK_FIX = (("本鲸", "私"), ("小深", "小深"))

    def _translate(self, text: str) -> str:
        """中文 → 日语。走本地 m2m100 服务（127.0.0.1:8891）。"""
        url = str(self._cfg("translate_url", "http://127.0.0.1:8891/translate"))
        if not url:
            return ""
        src = text
        for a, b in self._SPEAK_FIX:
            src = src.replace(a, b)
        payload = json.dumps({"text": src[:400]}, ensure_ascii=False).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(self._cfg("translate_timeout", 90))) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaVoiceClient] 翻译失败：{exc}")
            return ""
        return str(data.get("ja") or "").strip()

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
        raw = "\n".join(line for line in raw.split("\n") if not line.lstrip().startswith((">", "```")))
        if not raw.strip():
            return

        # 日语原文直接念；纯中文先送去翻译服务，拿回日语再念
        jp_text = ""
        zh_text = raw
        if is_japanese(raw):
            segs = re.split(r"(?<=[。！？!?\n])", raw)
            jp_text = "".join(x for x in segs if _KANA.search(x)).strip()
            zh_text = "".join(x for x in segs if x.strip() and not _KANA.search(x)).strip() or raw
        else:
            translated = await asyncio.to_thread(self._translate, raw)
            jp_text = translated or ""

        if not jp_text:
            return
        text = jp_text
        raw = zh_text

        kept: list[str] = []
        limit = int(self._cfg("max_chars", 300) or 300)
        # 念的是日语（翻译结果），显示的是中文原文——别搞反
        voice_text = text.strip()[: limit * 2]
        pieces = split_chunks(voice_text, min(limit, 200))
        for piece in pieces:
            try:
                path = await asyncio.to_thread(self._ask_service, piece)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaVoiceClient] 调服务失败：{exc}")
                continue
            if path and os.path.exists(path):
                kept.append(path)
        if kept:
            # 语音在前，中文翻译在后
            plain_idx = next((i for i, c in enumerate(chain) if isinstance(c, Plain)), None)
            if plain_idx is not None:
                chain[plain_idx].text = raw
            for path in reversed(kept):
                chain.insert(0, Record(file=path))
            result.chain = chain
