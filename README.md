# 深海之声

语音条由本地两个服务合作产出：**翻译**（中文 → 日语）+ **合成**（日语 → 语音）。
插件本身很薄，只做调度和拼接。

- 插件目录：`data/plugins/astrbot_plugin_xiaoshen_local_tts/`
- 翻译服务：`data/translate/server.mjs`（Node + transformers.js + m2m100_418M）
- 语音服务：`data/tts_service/server.py`（Python + VOICEVOX CORE）
- 版本：0.1.0

## 链路

```
AI 回复（中文）
   ↓
插件：取正文，剔掉引用块 / 代码块
   ↓
翻译服务 :8891  中文 → 日语
   ↓
语音服务 :8890  日语 → mp3
   ↓
消息链：[语音] + [中文原文]
```

## 生命周期

翻译服务**由插件拉起**：加载插件时先探 8891 端口，没人就用 `node server.mjs` 起一个。
AstrBot 重启 → 插件重载 → 服务自动回来，不用手动敲命令。

语音服务（8890）仍需要自己常驻，或用 `data/tts_service/start.sh`。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | true | 总开关 |
| `service_url` | `http://127.0.0.1:8890/tts` | 语音合成服务 |
| `translate_url` | `http://127.0.0.1:8891/translate` | 翻译服务；填空则不翻译，只对日语原文配音 |
| `translate_timeout` | 90 | 翻译超时（第一次加载模型慢） |
| `max_chars` | 300 | 单条上限（约 75 秒语音） |
| `timeout` | 120 | 合成超时 |
| `chinese_too` | false | 兼容旧行为，现在中文统一走翻译 |

## 说话习惯

翻译前会把「本鲸」替换成「私」，否则 m2m100 会把它译成 **ウサギ（兔子）**。
映射表在 `main.py` 的 `_SPEAK_FIX`。

## 排错

```bash
curl http://127.0.0.1:8891/health   # 翻译服务
curl http://127.0.0.1:8890/health   # 语音服务

grep -E "翻译失败|调服务失败" data/logs/astrbot.log | tail
tail -5 /tmp/translate.log
tail -5 /tmp/tts_service.log
```

常见现象：

- **没有语音**：先看两个 `/health`，再看插件是否在白名单
- **文字变成「是。」之类**：变量串了（历史 bug，已修：念日语、显示中文）
- **翻译超时**：第一次加载要 20 秒左右，`translate_timeout` 别设太小
- **服务莫名消失**：运存不够被系统杀，`node --max-old-space-size` 别低于 1024
