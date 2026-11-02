# 本仓库的修改说明

本仓库基于 [xiaoxi2760/astrbot_plugin_denia_share](https://github.com/xiaoxi2760/astrbot_plugin_denia_share)（希望解析器，MIT 许可）修改，**仅改动了一处功能**，其余代码与上游 v1.3.1 保持一致。原项目及其许可证、致谢信息原样保留，见 [LICENSE](LICENSE) 与 [LICENSES/](LICENSES/)。

## 修改内容：多图图集改用 QQ Markdown 渲染

对应提交 `fcc48f6`，共改动 5 个文件（+450 / -16 行）。

### 背景

图集类内容（如小红书、抖音多图笔记）图片较多时，原版会把图片打包成 `Comp.Nodes` 合并转发发送。但合并转发只有 **OneBot 协议端**支持——使用 QQ 官方机器人通道时图片发不出来。

### 改动

图片数量达到阈值时，不再打包合并转发，而是把图片 URL 组装成 **QQ Markdown 消息**集中展示。Markdown 正是 QQ 官方机器人能渲染图片的通道，因此该路径不依赖 `_is_onebot` 判断。

具体实现：

- `_try_send_media` 中 Markdown **优先于**合并转发；未命中条件或发送失败时自动回退到原有链路
- `ImageContent` 新增 `url` 字段，`create_images` / `create_image` 保留原始直链
- 新增 `_collect_markdown_images` / `_build_markdown_messages` / `_send_markdown_media` 三个辅助方法
- `_iter_media_components` 增加 `skip_images` 参数，避免图片被发送两遍
- QQ Markdown 图片必须写成 `![文字 #宽px #高px](url)` 格式，缺省尺寸会渲染成 `[image]` 占位符
- 等比缩放：读取本地已落盘文件的真实宽高计算显示尺寸，不拉伸不变形

### 新增配置项（7 个）

| 配置项 | 说明 |
| --- | --- |
| `MARKDOWN_IMAGE_ENABLED` | 是否启用 Markdown 图集渲染 |
| `MARKDOWN_IMAGE_THRESHOLD` | 触发阈值（图片数量） |
| `MARKDOWN_IMAGE_MAX_PER_MSG` | 单条消息最多渲染的图片数 |
| `MARKDOWN_IMAGE_PER_ROW` | 每行并排图片数 |
| `MARKDOWN_IMAGE_WIDTH` / `MARKDOWN_IMAGE_HEIGHT` | 图片显示尺寸上限 |
| `MARKDOWN_IMAGE_SIZE_MODE` | 尺寸模式（auto / 固定） |

配置同步维护在 `CONFIG_META`（`core/config.py`）与 `_conf_schema.json` 两处，通过 `verify_schema_alignment` 自检保证一致。

### 改动文件清单

| 文件 | 改动 |
| --- | --- |
| `main.py` | Markdown 收集 / 组装 / 发送逻辑，发送优先级调整（+223） |
| `core/config.py` | 7 个 `MARKDOWN_*` 配置项定义（+151） |
| `_conf_schema.json` | 配置面板 schema 同步（+72） |
| `core/data.py` | `ImageContent.url` 字段及构造函数传参（+12） |
| `core/base_parser.py` | 保留原始直链（+8） |

## 2026-11-03 更新：纯图网格展示

- Markdown 图集消息不再携带头部信息（平台 / 作者 / 标题），只输出纯图片网格
- 图片标签文字（alt）留空占位，QQ 端只渲染图片本身，不再显示大字号标签
- 消息末尾追加一行块引用小字，说明 QQ 保存去重误判的处理方式：

  > 保存时若提示已保存，直接点击再次保存即可

  （QQ 客户端对整条 Markdown 消息按「已保存」去重时存在误判，实际未落盘，
  再次保存即可正常下载，插件侧无法绕过）
