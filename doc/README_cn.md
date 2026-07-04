# thucloud 中文说明

`thucloud` 是一套面向清华云盘的大文件操作工具，提供命令行工具和 Python 库接口。

项目重点支持以下工作流：

- 从清华云盘资料库或分享链接下载大文件；
- 将本地大文件或目录上传到清华云盘资料库；
- 通过本机作为临时中转，将外部数据集 URL 上传到清华云盘；
- 自动把超大上传拆分为可恢复的 `.partNNN` 分片；
- 对临时网络错误和云盘后端错误进行安全重试。

命名关系：

- 发行包名：`thucloud-toolkit`
- Python 导入包名：`thucloud`
- 命令行程序名：`thucloud`

## 安装

日常使用时，建议把项目作为安装包安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

查看命令帮助：

```bash
thucloud --help
```

开发调试时，也可以在源码目录中使用 `python3 -m thucloud --help`。

## 身份认证

推荐通过环境变量传入 Web API Auth Token，避免 token 进入 shell 历史记录：

```bash
export THUCLOUD_TOKEN=<your_web_api_auth_token>
```

列出当前账号可见的资料库：

```bash
thucloud repos
```

如果 token 曾经被粘贴到聊天、日志或脚本中，建议在网页端重新生成并轮换 token。

## 常用命令

列出远端目录：

```bash
thucloud ls \
  --repo-id <library-id> \
  --remote-dir /behave
```

上传本地文件或目录：

```bash
thucloud upload \
  --repo-id <library-id> \
  --remote-dir /datasets/behave \
  ./Date03.zip
```

从文本文件读取 URL，并通过本机中转上传到云盘：

```bash
thucloud relay \
  --repo-id <library-id> \
  --remote-dir /datasets/behave \
  --links-file deprecated/links.txt \
  --split-size-gb 1 \
  --staging-mode stream
```

网络不稳定时，可以先把每个分片下载到本地缓存，再上传到云盘：

```bash
thucloud relay \
  --repo-id <library-id> \
  --remote-dir /datasets/behave \
  --links-file deprecated/links.txt \
  --split-size-gb 1 \
  --staging-mode cache \
  --cache-dir .cache/thucloud \
  --max-cache-gb 2
```

从资料库下载文件：

```bash
thucloud download \
  --repo-id <library-id> \
  -o downloads \
  /datasets/behave/Date03.zip.part000
```

从清华云盘分享链接下载文件：

```bash
thucloud share-download \
  --share-url https://cloud.tsinghua.edu.cn/d/<share-key>/ \
  --include "*.zip" \
  -o downloads \
  -y
```

## 大文件行为

上传文件超过 `--split-size-gb` 时，工具会把它拆成多个独立文件：

```text
Date03.zip.part000
Date03.zip.part001
Date03.zip.part002
```

下载所有分片后，可以用下面的命令重新合并：

```bash
cat Date03.zip.part* > Date03.zip
```

默认参数偏向可靠性：

- `--split-size-gb 1`
- `--retries 5`
- `--skip-existing`
- `--resume`
- `--verify-upload`
- `--upload-timeout-sec 600`

`relay` 命令并不是让清华云盘服务器直接拉取第三方 URL。清华云盘当前上传接口需要客户端提交 multipart 数据，因此本工具会使用本机作为传输客户端：

- `--staging-mode stream`：数据从外部 URL 读取后，经内存流式上传到云盘；
- `--staging-mode cache`：每个分片先写入 `.cache/thucloud/parts`，上传成功后删除，除非指定 `--keep-cache`。

本地上传需要更严格校验时，可以开启 `--checksum-source`。工具会在每个分片上传前后计算 SHA-256；如果源文件在传输过程中变化，命令会停止并把对应 manifest 标记为失败，后续运行会重新上传而不是只信任远端同大小文件。

`relay --staging-mode cache` 默认会在开始前清理过期的 `*.tmp` 缓存文件。可通过 `--no-cleanup-cache`、`--cache-ttl-hours` 或 `--keep-cache` 调整缓存保留策略。

## 常见错误

- `413 Request Entity Too Large`：单次上传请求过大，降低 `--split-size-gb`。
- `403 Permission denied`：token、资料库权限或上传目录配置有问题。
- `403 Access token not found`：临时上传端点失效，工具会重新获取 upload-link 并重试。
- `500 Internal error`：清华云盘后端处理某个分片失败，重试或降低 `--split-size-gb`。
- `SSL unexpected eof`：TLS 连接在传输中断开，重试或使用 `--staging-mode cache`。
