#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深海之声 · 一体化版。

插件内直接调 VOICEVOX 合成，没有外部 HTTP 服务——不会出现"服务掉了没声音"。

规则：回复写成「日语 + 中文」两段 → 日语合成语音，中文留在文字里。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

try:
    from voicevox_core.blocking import (
        Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile,
    )
except ImportError:  # pragma: no cover
    Onnxruntime = OpenJtalk = Synthesizer = VoiceModelFile = None

KANA = re.compile(r"[\u3040-\u30ff]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?])")
# 句号 0.04 / 问叹分号 0.03；逗号不念不停
GAP = {"。": 0.04, ".": 0.04, "？": 0.03, "?": 0.03, "！": 0.03, "!": 0.03, "；": 0.03, ";": 0.03}


def has_kana(text: str) -> bool:
    return bool(KANA.search(text or ""))


class DeepseaVoice(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(self.plugin_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._synth = None
        self._loaded = ""
        self._lock = asyncio.Lock()
        self._last_use = time.time()
        self._idle: asyncio.Task | None = None

    # ---------------- 配置 ----------------
    def _cfg(self, key, default=None):
        value = self.config.get(key)
        return default if value in (None, "") else value

    def _base(self) -> str:
        return str(self._cfg("voicevox_dir", "/root/AstrBot/data/voicevox"))

    def _ready(self) -> bool:
        if Synthesizer is None:
            return False
        base = self._base()
        return all(os.path.exists(p) for p in (
            os.path.join(base, "dict", "open_jtalk_dic_utf_8-1.11"),
            os.path.join(base, "models", "vvms", str(self._cfg("voicevox_model", "0.vvm"))),
            os.path.join(base, "onnxruntime", "lib", "libvoicevox_onnxruntime.so.1.17.3"),
        ))

    # ---------------- 引擎 ----------------
    def _engine(self) -> Synthesizer:
        if self._synth is not None:
            return self._synth
        base = self._base()
        ort = Onnxruntime.load_once(
            filename=os.path.join(base, "onnxruntime", "lib", "libvoicevox_onnxruntime.so.1.17.3"))
        ojt = OpenJtalk(os.path.join(base, "dict", "open_jtalk_dic_utf_8-1.11"))
        self._synth = Synthesizer(ort, ojt)
        return self._synth

    def _load_model(self) -> None:
        path = os.path.join(self._base(), "models", "vvms", str(self._cfg("voicevox_model", "0.vvm")))
        if self._loaded == path:
            return
        style = int(self._cfg("voicevox_style", 3))
        self._engine().load_voice_model(VoiceModelFile.open(path))
        self._loaded = path
        logger.info(f"[深海之声] 模型已加载 style={style}")

    def _synthesize(self, text: str) -> str:
        """日语 → mp3 路径。按句子切分，自己插静音。"""
        style = int(self._cfg("voicevox_style", 3))
        key = hashlib.sha1(f"{text}|{style}".encode()).hexdigest()[:16]
        out = os.path.join(self.cache_dir, f"{key}.mp3")
        if bool(self._cfg("cache", True)) and os.path.exists(out):
            return out

        self._load_model()
        e = self._engine()
        segs = [x for x in SENT_SPLIT.split(text) if x.strip()] or [text]

        chunks: list[tuple[bytes, float]] = []
        for seg in segs:
            try:
                q = e.create_audio_query(seg, style)
            except Exception:            # 单句解析不了就跳过
                continue
            q.speed_scale = float(self._cfg("speed", 1.05))
            q.intonation_scale = float(self._cfg("intonation", 1.0))
            q.pre_phoneme_length = 0.04
            q.post_phoneme_length = 0.06
            try:
                chunks.append((e.synthesis(q, style), GAP.get(seg.rstrip()[-1], 0.04)))
            except Exception:            # noqa: BLE001
                continue
        if not chunks:
            raise RuntimeError("没有可念的内容")

        wav = self._mix(chunks)
        return self._to_mp3(wav, out)

    @staticmethod
    def _mix(chunks: list[tuple[bytes, float]]) -> bytes:
        pcm: list[bytes] = []
        gaps: list[bytes] = []
        rate, width = 24000, 2
        for blob, gap in chunks:
            with wave.open(io.BytesIO(blob)) as fh:
                rate, width = fh.getframerate(), fh.getsampwidth()
                pcm.append(fh.readframes(fh.getnframes()))
            gaps.append(b"\x00" * (int(rate * gap) * width))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(width)
            fh.setframerate(rate)
            for i, data in enumerate(pcm):
                fh.writeframes(data)
                if i < len(gaps):
                    fh.writeframes(gaps[i])
        return buf.getvalue()

    def _to_mp3(self, wav_bytes: bytes, out: str) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("缺 ffmpeg")
        src = os.path.join(tempfile.gettempdir(), f"dsv_{os.getpid()}_{os.urandom(3).hex()}.wav")
        with open(src, "wb") as fh:
            fh.write(wav_bytes)
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", src,
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,volume=0dB,alimiter=limit=0.95",
                 "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame",
                 "-b:a", str(self._cfg("bitrate", "96k")), out],
                check=True, timeout=180)
        finally:
            try:
                os.remove(src)
            except OSError:
                pass
        os.chmod(out, 0o644)
        self._clean_cache()
        return out

    def _clean_cache(self, keep: int = 80) -> None:
        try:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir)]
            files = [f for f in files if os.path.isfile(f)]
            if len(files) > keep:
                files.sort(key=os.path.getmtime)
                for path in files[: len(files) - keep]:
                    os.remove(path)
        except OSError:
            pass

    # ---------------- 空闲释放 ----------------
    def _ensure_idle(self) -> None:
        if self._idle is None or self._idle.done():
            try:
                self._idle = asyncio.create_task(self._idle_loop())
            except RuntimeError:
                self._idle = None

    async def _idle_loop(self) -> None:
        while True:
            minutes = float(self._cfg("idle_unload_minutes", 15) or 0)
            await asyncio.sleep(60)
            if minutes <= 0 or self._synth is None or self._lock.locked():
                continue
            if time.time() - self._last_use < minutes * 60:
                continue
            self._synth = None
            self._loaded = ""
            logger.info(f"[深海之声] 闲置 {minutes:.0f} 分钟，释放模型")

    # ---------------- 钩子 ----------------
    @filter.on_decorating_result()
    async def decorate(self, event: AstrMessageEvent):
        if not bool(self._cfg("enable", True)) or not self._ready():
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
        raw = plain.text or ""
        segs = [x for x in re.split(r"(?<=[。！？!?\n])", raw) if x.strip()]
        jp = "".join(x for x in segs if has_kana(x)).strip()
        zh = "".join(x for x in segs if not has_kana(x)).strip()
        if not jp:
            return                      # 没有日语，就只发文字

        guard = int(self._cfg("guard_memory_mb", 200) or 0)
        if guard > 0:
            avail = self._available_mb()
            if 0 <= avail < guard:
                logger.warning(f"[深海之声] 可用运存 {avail:.0f}MB < {guard}MB，跳过配音")
                return

        self._last_use = time.time()
        self._ensure_idle()
        try:
            async with self._lock:
                path = await asyncio.wait_for(
                    asyncio.to_thread(self._synthesize, jp[:400]),
                    timeout=float(self._cfg("timeout", 120) or 120))
        except asyncio.TimeoutError:
            logger.warning("[深海之声] 合成超时")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[深海之声] 合成失败：{exc}")
            return

        if path and os.path.exists(path):
            if zh:
                plain.text = zh
            chain.insert(0, Record(file=path))
            result.chain = chain

    @staticmethod
    def _available_mb() -> float:
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            pass
        return -1.0

    async def terminate(self):
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        self._synth = None
        logger.info("[深海之声] 收声。")
