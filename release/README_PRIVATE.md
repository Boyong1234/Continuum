# Continuum 私人迁移目录

**敏感私人包：含聊天、Key、来源和备份，不能上传为公开源码。** 以release-manifest.json的lineage.active_dataset为唯一当前档案；其他v3/历史副本不自动合并。

## 生成和搬运

在原项目执行：

```powershell
runtime/envs/backend/Scripts/python.exe -B scripts/build_release.py --kind private --destination 一个不存在的新目录 --include-runtime
```

使用既有一致性SQLite备份/恢复机制，短暂保留写者槽位以取得DB与Persona稳定快照，不停止日常服务。快照时刻之后的新聊天不会倒流进入这个包；**真正迁移切换时应正常停止旧服务，再生成最后一份快照**，只运行一个权威副本。

保留当前Archive、Episode/Core、Evidence、Revision/Lifecycle、队列/游标/账本、Persona、DeepTalk原件/经验索引、私人配置与必要备份。旧PID、日志进程状态、登录任务实例不恢复。旧lineage作为历史材料保留。第三方upstream/缓存/专有组件、QQ会话不复制，压缩的旧发行归档单列在清单排除项；不会静默宣称已带齐第三方运行程序。

复制此目录到新电脑硬盘后双击`启动 Continuum.cmd`。带Runtime时启动器按当前目录修复两套venv，保留依赖/许可证，无需Codex；没有包内Runtime则安装官方Python后按prepare_source.ps1准备。持有文件夹的人可以读取明文Key和历史。

## QQ与连续性

安装官方QQ，按SnowLuma许可自行准备组件并扫码，不能依赖旧电脑AppData。包内只生成新的AstrBot gateway-only配置/Bridge和供手动填写的OneBot配置，不含SnowLuma本体或认证状态。SnowLuma反向WS目标为`ws://127.0.0.1:6199/ws`，Token见本地AstrBot配置；账号/主人和Bridge绑定沿原配置保留。

先正常停止旧电脑Bot/watch，避免两份Bot同时响应。原电脑登录任务属于原目录，新目录不接管、不删除它；同电脑检测到原登录任务时启动器会提示显式切换，而不是启动第二个watch。新电脑沿保留的Memory配置/授权和24小时低频策略启动。旧lease会按原超时恢复，队列/checkpoint不清空。

四个根CMD负责启动、停止、状态、记忆。必要时用`portable.ps1 -Action Configure`重新配置账号/Key，不清历史。暂停/恢复用`auto_memory.ps1 -Action Pause/Resume`。启动/停止只管理当前目录自己的进程，不退出系统QQ，不删WAL或登录缓存。

## 验证边界

本轮进行了同机不同路径、包内Python修复、核心启动/停止、原有数据/记忆读取和Mock聊天验证；没有切换原QQ，没有真实DeepSeek调用。**尚未完成跨机器验证**，也未在新电脑验收QQ扫码及收发。保留的Style Anchor/纠正开关不会因打包自动启用；最新Episode时间线代码已包含，但是否切换日常运行仍由用户决定。

许可与外部依赖见release/THIRD_PARTY.md。不要把本包视为第三方组件的公开再分发授权。
