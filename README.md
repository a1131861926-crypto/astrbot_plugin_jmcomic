# astrbot_plugin_jmcomic

AstrBot 插件：在 OneBot v11（`aiocqhttp`）接入下，通过聊天指令提交 JMComic 本子编号，由服务端下载图片，默认合成为一个 PDF 文件后上传回当前对话。

## 使用方法

```text
/jm 123456
/jmcomic JM123456
/本子 123456
```

默认保存目录为 AstrBot 工作目录下的 `data/jmcomic/`：

- `data/jmcomic/albums/`：临时图片目录
- `data/jmcomic/archives/`：最终 PDF 或 zip 文件

## 配置项

插件提供 `_conf_schema.json`，可在 AstrBot 插件配置页面修改：

- `output_format`：上传文件格式，默认 `pdf`；设为 `zip` 可上传图片压缩包。
- `upload_filename_mode`：上传显示文件名模式，默认 `random`；也可选 `timestamp` 或 `original`。
- `upload_filename_prefix`：随机名或时间戳文件名的前缀，默认 `document`。
- `client_impl`：JMComic 客户端实现，可选 `api` 或 `html`。
- `proxy`：网络代理，例如 `system`、`null`、`127.0.0.1:7890`。
- `max_file_mb`：允许发送的最大文件大小，超过限制时只保留本地文件，不上传。
- `extra_option_yaml`：追加 jmcomic 高级配置，例如 cookies、域名、插件等。

## 依赖

插件依赖写在 `requirements.txt`：

```text
jmcomic>=2.7.0
```

`jmcomic` 会依赖 Pillow，本插件使用 Pillow 将下载图片合并为 PDF。

## 开源协议

本项目基于 MIT License 开源，详见 [LICENSE](LICENSE)。

请仅在符合法律法规、平台规则和内容授权的前提下使用本插件。
