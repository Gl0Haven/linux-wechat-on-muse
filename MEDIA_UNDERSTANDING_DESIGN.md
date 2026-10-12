# 图片理解 + 表情包功能设计稿（2026-10-09）

## 一、现状

- `wal_reader_daemon.py` 已识别 `msg_type=3`（图片）→ `[图片]`、`msg_type=47`（表情）→ `[表情]`，但只做上下文占位，**不进回复队列**
- 图片文件：`~/xwechat_files/wxid_YOUR_BOT_ACCOUNT/msg/attach/<md5>/`
- 表情文件：`~/xwechat_files/wxid_YOUR_BOT_ACCOUNT/resource/emoticon_webp/*.webp`
- 2026-10-06 实测：[friend1] 发图 @MUSE 求评价，bot 只能回"看不见"

## 二、别人怎么做（调研结论）

### 2.1 wfxm 项目（微信 Butler 网关，生产级）

核心模式：**intake → 理解 → 注入**，三段解耦

```
图片下载到本地缓存 → MiniMax VLM 识图转文字 →
注入格式：
[微信图片]
（用户附图说明：帮看下这个报错截图）

--- 图片识别 ---
（OCR/视觉摘要：界面显示 xxx 错误码 …）
```

关键设计点：
- **不把图片直接喂给对话 LLM**，而是先转成文字描述，再走原有文本 pipeline（分类、回复逻辑零改动）
- 有 caption 时保留 caption，识别结果作补充
- 识别失败时降级为提示句，不阻塞网关

### 2.2 wechatauto / WeFlow（解密层）

- Windows 版图片是 `.dat` 加密存储，需从微信进程内存抓 AES key 解密
- ~~Linux 版（我们的情况）直接是明文文件，无需解密~~
  **【2026-10-09 更正：错的】**实测 Linux 版 `msg/attach/.../Img/*.dat`
  前 16 字节是私有头（`07 08 56 32 …`），不是 JPEG/PNG 明文，同样加密。
  **截图兜底是目前唯一可行的看图方式。**
- 表情包同理，需走截图链路

### 2.3 通用架构模式

```
[微信客户端]
    → 消息 DB（含 msg_type + 媒体引用）
    → intake：按引用取本地文件
    → 理解：VLM/视觉模型转文字
    → 注入：文本描述进消息队列
    → 复用现有文本回复链路
```

## 三、我们的设计思路

### 核心洞察

**不需要外部 VLM API**——worker 本身就是 Muse agent，我原生支持 vision。
图片文件传给 worker，我直接"看"图做裁决，零外部依赖、零费用、不泄露隐私。

### Phase 1：图片理解（收）

**1. intake（wal_reader_daemon.py）**
- `msg_type=3` 的消息：从 content blob 解析图片标识（MD5/路径）
- 在 `msg/attach/<md5>/` 找到文件，拷贝到 `~/workspace/wechat-bridge/media/<msg_id>.jpg`
- 注意：微信可能先写 DB 后落盘，文件不存在时重试等待（最多 30 秒）
- queue.db 新增 `media_path` 列，存本地路径；`should_reply` 按现有规则分类
  - 有人发图并 @bot / 大号发图 → EXPLICIT（1）
  - 群聊普通图片 → 0（只存档，供上下文）

**2. 理解（worker）**
- `worker_batch.py` JSON 输出加字段：`has_image: bool`、`media_path: str`
- worker 领到带图消息后，把图片文件作为输入传给我，我直接看图
- 我在裁决时看到的是：文字（caption）+ 图片内容，一起判断回什么

**3. 注入**
- 不需要单独的"转文字"步骤（wfxm 需要是因为他们的 LLM 是纯文本的）
- 但为了**上下文可回溯**（recent_5min 里别人看不到图），worker 回复后把我对图片的一句话描述写入 `chat_memory`
- 格式：`[图片：一只橘猫坐在键盘上]`

**4. 降级**
- 图片文件找不到/损坏 → 回退到现在的 `[图片]` 占位，不阻塞流程

### Phase 2：表情包理解（收）

**1. intake**
- `msg_type=47` 的 content 是 XML，解析出表情 ID / MD5
- 在 `resource/emoticon_webp/` 找到对应 `.webp`，转码为 PNG
- 存到 `media/`，同图片流程

**2. 理解**
- 我直接看表情图，理解语义（搞笑/嘲讽/附和/哭笑不得等）
- 结合上下文判断：比如群友互损时发的"狗头"表情 = 站队起哄信号

**3. 注入**
- 格式：`[表情：捂脸哭笑]`
- 表情不单独触发回复，但作为上下文信号影响裁决（比如有人发嘲讽表情 @bot，倾向接梗）

### Phase 3：表情包发送（发）

**1. 表情库**
- 建 `games/` 类似的 SQLite 表：`sticker_id` → 语义标签（哈哈/嘲讽/ok/哭/狗头等）
- 初期只收录 10-20 个高频表情，人工标注

**2. 发送**
- worker 生成回复时可附带 `sticker_id`
- sendd 通过 GUI 表情面板发送（点击表情按钮 → 搜索/选择 → 发送）
- 宁缺毋滥：不确定用哪个时宁可纯文字

### 四、风险与取舍

| 风险 | 对策 |
|------|------|
| 图片落盘延迟 | intake 重试 30 秒，超时则降级占位 |
| 大图/多图性能 | 只在 EXPLICIT 时才传图给 worker；群聊普通图片只存档 |
| 表情 XML 解析失败 | 降级为 `[表情]` 占位 |
| GUI 发表情不稳定 | Phase 3 先只做收不做发，发送等收稳定后再上 |
| 隐私 | 图片只存本地，不调任何外部 VLM API |

### 五、实施顺序

1. **Phase 1**（图片收）：wal_reader intake + media_path 列 + worker_batch.py 透出 + worker 看图回复
2. **Phase 2**（表情收）：XML 解析 + webp 转码 + 同流程接入
3. **Phase 3**（表情发）：表情库 + sendd GUI 发送（可选，看前两个的效果）

### 六、与现有架构的兼容

- 不改 `should_reply` 三态逻辑，只增加图片/表情的分类规则
- 不改 claim/commit/fencing，媒体只是消息的附加属性
- `worker_batch.py` 加字段是向后兼容的（老 cron body 忽略新字段）
- scheduler 的 zombie 清理不影响（media 文件由 intake 管理生命周期）
