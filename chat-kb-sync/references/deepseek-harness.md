# DeepSeek Harness 专用采集适配器

读取用户明确选定的 Harness profile 的 `sessions` 目录。它只适用于交接中使用的本机 v4 格式：`.../session-<UUID>/session.v4.jsonl.zstd`。不要把系统盘、整个家目录、其他账号的目录、聊天备份或凭据目录作为扫描根。profile 根路径以该用户实际安装为准；默认路径不等于已获准读取。

## 接入

1. 默认使用明确批准的聊天清单。先从 Harness 本身或已有可信本机清单确认归属和完整的 `session-<UUID>` 目录名。清单不进 Git；空清单不采集。源文件路径仅用于本机访问，不传给后续摘要服务。
2. 为使用本适配器的 Python 环境另装可选的 [python-zstandard](https://python-zstandard.readthedocs.io/en/latest/decompressor.html)。浏览、通用导出和 Codex 适配器不依赖它。使用支持 `stream_reader(..., read_across_frames=True)` 的版本。
3. 通过 `dsh_source.collect(root, allowed_ids, min_idle_seconds=600)` 获取共用会话记录。定时运行默认建议至少静默 10 分钟；文件正在修改时，跳过本轮，不删除上次的总结。修改时间来自源文件（UTC），不是导入时间。
4. 对第一批用合成样例验证：至少两个独立 zstd 帧能被读到，真人发言与插入副本只保留一份，注入内容及工具输出被排除，改写旧消息会保留消息 ID 并变更正文。只在允许后验证真实资料。

## 自动发现新聊天

仅当用户明确授权持续扫描自己独用的 Harness profile 时，才选配置模式 `owned-dsh-profile`。此模式的 `dsh_source.discover(root)` 只看目录名、文件名与文件属性，不读正文，发现后仍由采集器核对 v4 文件头、身份一致性和主会话深度。v4 格式本身不能证明用户身份；个人专用 profile 必须由用户确认，不能从目录名推断。混合账号、共享电脑上的共享 profile、公司或家庭共用 profile 继续用逐条清单。数据根路径、模式或授权范围变更后重新核对，不能默默把已有授权扩展到另一个 profile。

## v4 识别与边界

- 主会话头须为 `type=session`，记录 ID 与被批准的目录名一致，且 `delegationDepth=0`；子代理和无法确认深度的旧格式不自动入库。
- 仅采集 `user/message`、`agent/inbox/spliced` 中 `source.kind=user` 的用户文本，以及 `assistant/message` 的助手文本块。用户消息优先用原事件副本，按消息 ID 去重；可用时保留来源顺序和序号。系统指令、运行时注入、工具调用/回显、推理块、附件和会话标题不参与采集；没有验证工具证据时，不能把助手自述当成已实际执行。
- 文件按多帧 zstd 流读取，不用只读第一帧的单次解压接口。超过大小/行数限制、无法解析、重解析点/符号链接/硬链接、同一 ID 多份文件或读取期间修改，均保守跳过。限额是保护，不代表一切旧/长聊天都受支持。`on_skip` 可选回调只报告固定原因码。
- 采集不调用 DeepSeek，也不读取 Harness 凭据，不启动它的计划任务。摘要是否使用云模型要另外决定。换 profile、换版本或迁移已有入库状态时，先做本机试点；不要把不同账号的记录和状态文件直接混用。
