# 深海之声

仓库：<https://github.com/lengqiu08/astrbot_plugin_xiaoshen_local_tts>

小深的重置版语音台。只做一件事：**把 AI 的回复念成语音条，跟着文字一起发出去。**

全程本地 onnx（kokoro），不连云、不花钱。

- 插件目录：`data/plugins/astrbot_plugin_xiaoshen_local_tts/`
- 内部注册名：`astrbot_plugin_xiaoshen_local_tts`（沿用旧名，AstrBot 的 `plugin_set` 白名单按内部名匹配）
- 面板显示名：深海之声
- 版本：1.0.0

---

## 一、它怎么工作

挂在 `on_decorating_result` 上：AI 回复发出前，读出文字 → 清掉动作描写和 markdown → 判断语种 → 本地合成 → 在消息链末尾追加一条 `Record`。

不产生指令、不接管聊天，只在回复上加一条语音。

## 二、支持的语种

| 语种 | 音色 | 说明 |
| --- | --- | --- |
| 中文 | `zf_xiaoxiao` | 走 misaki 音素化，比 espeak 准（"游了一圈"不会被念成"居然丽已"） |
| 英文 | `af_heart` | `bf_/bm_` 开头是英音，会自动匹配口音 |
| 日文 | `jf_alpha` | 走 misaki.ja（需要 pyopenjtalk），espeak 的 ja 音素会把日语念成乱码 |
| 法文 | `ff_siwis` | 只有一个音色 |
| 西班牙文 | `ef_dora` | |
| 意大利文 | `if_sara` | |
| 葡萄牙文 | `pf_dora` | |
| 印地文 | `hf_alpha` | |

韩语、德语、俄语、阿拉伯语**没有 kokoro 音色**，会直接跳过（不发语音），不会退化成 espeak 怪音。

## 三、配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | true | 总开关 |
| `model_dir` | `/root/xiaoshen_models/kokoro` | 要有 `kokoro-v1.0.onnx` + `voices-v1.0.bin` |
| `voice_zh` / `voice_en` / … | 见上表 | 各语种音色，留空用默认 |
| `speed` | 1.0 | 语速 |
| `volume_gain` | 6.0 | 响度归一后再加多少 dB，太炸调 2~3 |
| `bitrate` | 96k | 输出 mp3 码率 |
| `chunk_chars` | 80 | 长回复按句切段，每段多少字 |
| `max_parts` | 8 | 最多合成几段，超出的不念 |
| `max_chars` | 300 | 原文超过就截断 |
| `strip_brackets` | true | 去掉（尾鳍一甩）这类动作描写 |
| `cache` | true | 同文本只合成一次，最多留 120 条 |
| `timeout` | 120 | 单条合成超时，超时只发文字 |
| `idle_unload_minutes` | 10 | 闲置多久释放模型。kokoro 常驻约 400MB，默认闲置 10 分钟放掉；设 0 常驻（下次开口快，但一直占内存） |

配置落盘：`data/config/astrbot_plugin_xiaoshen_local_tts_config.json`

## 四、和旧版（1.16.x）的区别

**保留**：本地合成、多语种、音色可换、缓存、长文切句、响度归一。

**去掉**：
- 聊天控制台命令（`tts status` / `tts set` / `tts test` 等）——改配置请走 WebUI 插件配置页
- 变调、多后端来回切换（sherpa / piper / kokoro 三选一那套）
- WebUI 面板
- 上下文看门狗、家政清理（那是音乐台旧版的东西，与本插件无关）

**修掉的坑**：
- 非中文文本前被加上中文语气词 `嗯，`，合成出来变成 "Chinese letter"
- `lang="en"` 传给 kokoro 会报错并退到 espeak 中文后端
- 中文用 espeak 的 `cmn` 音素化，读音不准

## 五、排错

```bash
# 插件是否加载（描述应是"重置版语音台"）
grep "Plugin astrbot_plugin_xiaoshen_local_tts" /root/AstrBot/data/logs/astrbot.log | tail -2

# 合成失败会留 warning
grep "DeepseaVoice" /root/AstrBot/data/logs/astrbot.log | tail -20

# 单独试一句
/root/AstrBot/.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "/root/AstrBot/data/plugins/astrbot_plugin_xiaoshen_local_tts")
from main import DeepseaVoice, clean_text, detect_lang
p = DeepseaVoice(None, {})
t = clean_text("（尾鳍一甩）试试这一句。")
print(p._synthesize(t, detect_lang(t)))
EOF
```

常见现象：
- **只发文字没语音** → 文本超 `max_chars`、语种不支持、或合成超时（看日志 `DeepseaVoice` 的 warning）
- **语音很炸** → `volume_gain` 调小
- **念错专有名词** → 换音色或改文本

## 六、回滚

旧版在 `data/plugins_disabled/astrbot_plugin_xiaoshen_local_tts_legacy/`。
把当前目录挪出去、旧目录挪回来、WebUI 重载即可。旧配置备份在 `/tmp/tts_config_backup.json`。
