# 深海之声

只有 AI 回复是**日语**（或按配置的中文）时，才附一条本地合成的语音条。

插件很薄：**自己不加载任何模型**，只把文本 POST 给本地语音服务，拿回 mp3 挂到消息链上。
所以它不会吃内存，也不会有"缺模型"这类报错。

- 插件目录：`data/plugins/astrbot_plugin_xiaoshen_local_tts/`
- 语音服务：`data/tts_service/`（VOICEVOX，见那边的 README）
- 版本：0.0.1（测试版）

## 工作方式

```
AI 回复 → on_decorating_result
        → 判断是不是日语（有假名）
        → POST http://127.0.0.1:8890/tts
        → 拿到 mp3 → 追加 Record → 一起发出
```

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | true | 总开关 |
| `service_url` | `http://127.0.0.1:8890/tts` | 语音服务地址 |
| `max_chars` | 300 | 单条上限（约 75 秒语音），超了截断 |
| `timeout` | 120 | 请求超时（秒）。超过 120 秒当超时处理，只发文字 |
| `chinese_too` | true | 中文也用日语引擎念（汉字音读，日式中文） |

## 依赖

- 本地语音服务跑起来：`cd data/tts_service && python server.py`
- 无需任何云服务、API key

## 排错

```bash
curl http://127.0.0.1:8890/health      # 服务活着吗
grep DeepseaVoiceClient data/logs/astrbot.log | tail -10
```

- **没有语音**：先看 `/health`，再看插件是不是被 `plugin_set` 白名单挡了
- **中文也念**：`chinese_too` 关掉就只对日语配音
- **声音断断续续**：问题在服务那边（停顿参数），不在插件
