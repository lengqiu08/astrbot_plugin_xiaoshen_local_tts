#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深海之声 —— 把回复变成语音条。

只做一件事：AI 的回复落成一条本地合成的语音，跟着文字一起发出去。
后端以 kokoro 为主（音色自然），韩语/德语走 piper，不支持的语种直接跳过，
不再往 espeak 兜底里丢中文语气词（那是以前"Chinese letter"的来源）。

几条经验写在这儿，省得以后再踩：
· kokoro 的 lang 必须是 en-us / en-gb / fr-fr / pt-br 这种，传 "en" 会报错；
· 音色名首字母决定口音（af_=美音、bf_=英音、jf_=日语…），换音色要跟语种对得上；
· 中文文本里夹的英文、日文假名，中文音色念不了，先剔掉；
· 合成完统一走一遍 ffmpeg（裁静音 + loudnorm + 增益），响度才跟以前一致。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

try:
    from kokoro_onnx import Kokoro
except ImportError:  # pragma: no cover
    Kokoro = None

try:
    from misaki import zh as misaki_zh   # 中文音素，比 espeak 的 cmn 准得多
except ImportError:  # pragma: no cover
    misaki_zh = None

try:
    from misaki import ja as misaki_ja   # 日语音素，espeak 的 ja 会念成乱码
except ImportError:  # pragma: no cover
    misaki_ja = None

RATE = 24000
PLUGIN_NAME = "astrbot_plugin_deepsea_voice"

# 语种 -> (kokoro 音素化语种, 默认音色)
KOKORO_LANG = {
    "zh": "cmn", "en": "en-us", "ja": "ja", "fr": "fr-fr",
    "es": "es", "it": "it", "pt": "pt-br", "hi": "hi",
}
DEFAULT_VOICE = {
    "zh": "zf_xiaoxiao", "en": "af_heart", "ja": "jf_alpha", "fr": "ff_siwis",
    "es": "ef_dora", "it": "if_sara", "pt": "pf_dora", "hi": "hf_alpha",
}
# 音色名首字母 -> 口音（kokoro 的命名规则）
VOICE_ACCENT = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "cmn",
}

_MARKUP = re.compile(r"&&\s*[A-Za-z_]+\s*&&")          # 人格里的情绪标记
_BRACKETS = re.compile(r"[（(\[【][^）)\]】]{0,40}[）)\]】]")
_MARKDOWN = re.compile(r"[*_`#>~|]+")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]+"
)
_SPACE = re.compile(r"\s+")
_CN_PUNCT = str.maketrans({
    "，": "，", "。": "。", "！": "！", "？": "？",
    "：": "：", "；": "；", "、": "，",
})


def detect_lang(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def clean_text(text: str, strip_brackets: bool = True) -> str:
    text = _MARKUP.sub("", text or "")
    text = _URL.sub("", text)
    if strip_brackets:
        text = _BRACKETS.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _EMOJI.sub("", text)
    text = text.translate(_CN_PUNCT)
    text = _SPACE.sub(" ", text).strip()
    return text.strip("，。！？、；：,.!?;:·—… ")


def split_sentences(text: str, chunk: int = 80, max_parts: int = 8) -> list[str]:
    """按句切，攒到 chunk 字左右一段；最多 max_parts 段（多了截断，别合成一本书）。"""
    parts = re.split(r"(?<=[。！？!?；;])|(?<=\n)", text)
    out, buf = [], ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(buf) + len(part) <= chunk:
            buf += part
        else:
            if buf:
                out.append(buf)
            buf = part
        if len(out) >= max_parts:
            break
    if buf and len(out) < max_parts:
        out.append(buf)
    return out[:max_parts]


class DeepseaVoice(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(self.plugin_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._kokoro = None
        self._zh_g2p = None
        self._ja_g2p = None
        self._lock = asyncio.Lock()
        self._warned_lang: set[str] = set()

    # ---------------- 配置 ----------------
    def _cfg(self, key, default):
        value = self.config.get(key, default)
        return default if value in (None, "") else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    def _model_dir(self) -> str:
        return str(self._cfg("model_dir", "/root/xiaoshen_models/kokoro"))

    def _voice_for(self, lang: str) -> str:
        cfg_voice = str(self._cfg(f"voice_{lang}", "") or "").strip()
        if cfg_voice:
            return cfg_voice
        return DEFAULT_VOICE.get(lang, DEFAULT_VOICE["en"])

    def _accent_for(self, lang: str, voice: str) -> str:
        """音素化口音：跟着音色走，别让英式音色配美式音素。"""
        alias = KOKORO_LANG.get(lang, "en-us")
        accent = VOICE_ACCENT.get((voice or "a")[:1].lower())
        if accent and accent.split("-")[0] == alias.split("-")[0]:
            return accent
        return alias

    # ---------------- 模型 ----------------
    def _load(self):
        if self._kokoro is not None:
            return self._kokoro
        if Kokoro is None:
            raise RuntimeError("没装 kokoro-onnx")
        model = os.path.join(self._model_dir(), "kokoro-v1.0.onnx")
        voices = os.path.join(self._model_dir(), "voices-v1.0.bin")
        for path in (model, voices):
            if not os.path.exists(path):
                raise RuntimeError(f"缺模型文件：{path}")
        self._kokoro = Kokoro(model, voices)
        return self._kokoro

    # ---------------- 合成 ----------------
    def _cache_path(self, text: str, lang: str, voice: str, speed: float) -> str:
        raw = f"{text}|{lang}|{voice}|{speed}|{self._cfg('volume_gain', 6)}"
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache_dir, f"{key}.mp3")

    def _synth_one(self, text: str, lang: str, voice: str, speed: float, out: str) -> str:
        kokoro = self._load()
        accent = self._accent_for(lang, voice)
        # 中文音色念不了假名/谚文/西里尔，先剔掉，免得念成一串怪音
        if lang == "zh":
            text = re.sub(r"[\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff\u0600-\u06ff]+", " ", text).strip()
        if not text:
            raise RuntimeError("剔掉非中文片段后没有可念的内容")
        if lang == "zh":
            # 中文走 misaki 音素化：espeak 的 cmn 会把"游了一圈"念成"居然丽已"
            if self._zh_g2p is None:
                if misaki_zh is None:
                    raise RuntimeError("没装 misaki[zh]，中文没法念")
                self._zh_g2p = misaki_zh.ZHG2P(version="1.0")
            phonemes = self._zh_g2p(text)
            if isinstance(phonemes, tuple):
                phonemes = phonemes[0]
            samples, rate = kokoro.create(phonemes, voice=voice,
                                          speed=speed or 1.0, is_phonemes=True)
        elif lang == "ja" and misaki_ja is not None:
            # 日语同理：espeak 的 ja 音素是坏的，走 misaki.ja（要 pyopenjtalk）
            if self._ja_g2p is None:
                self._ja_g2p = misaki_ja.JAG2P()
            phonemes = self._ja_g2p(text)
            if isinstance(phonemes, tuple):
                phonemes = phonemes[0]
            samples, rate = kokoro.create(phonemes, voice=voice,
                                          speed=speed or 1.0, is_phonemes=True)
        else:
            samples, rate = kokoro.create(text, voice=voice, speed=speed or 1.0, lang=accent)
        if samples is None or len(samples) == 0:
            raise RuntimeError("合成结果为空")
        wav = out + ".wav"
        import soundfile as sf
        sf.write(wav, samples, rate)
        return wav

    def _postprocess(self, wavs: list[str], out: str) -> str:
        """拼接 + 裁静音 + 响度归一 + 转 mp3。没 ffmpeg 就退成 wav。"""
        ffmpeg = shutil.which("ffmpeg")
        if len(wavs) == 1 and not ffmpeg:
            shutil.copyfile(wavs[0], out.replace(".mp3", ".wav"))
            return out.replace(".mp3", ".wav")
        if not ffmpeg:
            raise RuntimeError("缺 ffmpeg，无法输出 mp3")
        concat_list = out + ".txt"
        with open(concat_list, "w", encoding="utf-8") as fh:
            for wav in wavs:
                fh.write(f"file '{wav}'" + chr(10))
        gain = float(self._cfg("volume_gain", 6) or 0)
        filt = (
            # 开头留一小段静音（0.2s）：不裁首端，让播放器和耳朵都有个缓冲，
            # 第一个字才不会像被"掐"出来；结尾仍只削明显静音并留 0.1s。
            "adelay=200|200,"
            "areverse,"
            "silenceremove=start_periods=1:start_duration=0.15:start_threshold=-50dB:start_silence=0.10,"
            "areverse,"
            "apad=pad_dur=0.12,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11,volume={gain}dB,"
            "alimiter=limit=0.95"
        )
        cmd = [ffmpeg, "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", concat_list,
               "-af", filt, "-ar", "24000", "-ac", "1",
               "-c:a", "libmp3lame", "-b:a", str(self._cfg("bitrate", "96k")),
               out]
        subprocess.run(cmd, check=True, timeout=300)
        for path in wavs + [concat_list]:
            try:
                os.remove(path)
            except OSError:
                pass
        return out

    def _synthesize(self, text: str, lang: str) -> str:
        voice = self._voice_for(lang)
        speed = float(self._cfg("speed", 1.0) or 1.0)
        cache = self._cache_path(text, lang, voice, speed)
        if bool(self._cfg("cache", True)) and os.path.exists(cache):
            return cache
        parts = split_sentences(text, int(self._cfg("chunk_chars", 80) or 80),
                                int(self._cfg("max_parts", 8) or 8))
        wavs = []
        stamp = f"{os.getpid()}_{os.urandom(3).hex()}"
        for i, part in enumerate(parts):
            wavs.append(self._synth_one(part, lang, voice, speed,
                                        os.path.join(tempfile.gettempdir(), f"dsv_{stamp}_{i}.mp3")))
        try:
            out = self._postprocess(wavs, cache)
        except Exception:
            for wav in wavs:
                try:
                    os.remove(wav)
                except OSError:
                    pass
            raise
        self._clean_cache()
        return out

    def _clean_cache(self, keep: int = 120) -> None:
        try:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir)]
            files = [f for f in files if os.path.isfile(f)]
            if len(files) <= keep:
                return
            files.sort(key=os.path.getmtime)
            for path in files[: len(files) - keep]:
                os.remove(path)
        except OSError:
            pass

    # ---------------- 钩子 ----------------
    @filter.on_decorating_result()
    async def decorate_result(self, event: AstrMessageEvent):
        """回复发出前，追加一条语音。"""
        if not self.enabled:
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
        text = clean_text("".join(texts), bool(self._cfg("strip_brackets", True)))
        if not text:
            return
        max_chars = int(self._cfg("max_chars", 300) or 300)
        if len(text) > max_chars:
            text = text[:max_chars]

        lang = detect_lang(text)
        if lang not in KOKORO_LANG:
            if lang not in self._warned_lang:
                self._warned_lang.add(lang)
                logger.info(f"[DeepseaVoice] 不支持语种 {lang}，这条跳过配音")
            return

        try:
            async with self._lock:
                path = await asyncio.wait_for(
                    asyncio.to_thread(self._synthesize, text, lang),
                    timeout=float(self._cfg("timeout", 90) or 90))
        except asyncio.TimeoutError:
            logger.warning("[DeepseaVoice] 合成超时，跳过本条")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaVoice] 合成失败（{lang}）：{exc}")
            return

        if path and os.path.exists(path):
            chain.append(Record(file=path, text=text))
            result.chain = chain

    async def terminate(self):
        self._kokoro = None
        logger.info("[DeepseaVoice] 收声。")
