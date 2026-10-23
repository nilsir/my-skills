---
name: wecom-doc-publish
description: Use when publishing or updating local Markdown as WeCom documents for API, Admin or Mall, associating TAPD requirements, notifying a configured WeCom group after publication changes, or reading shared WeCom document links. 中文触发词：发布到企微、更新对接文档、发布测试说明、读取企业微信文档。
---

# 企微文档发布

本地 Markdown 是正文来源。用户只要求写文档时先保存到本地；明确要求发布或更新后执行同步。项目、文件、文档类型和 TAPD 能从上下文确定时，直接执行，不重复询问发布许可。

## 安装与配置

首次使用或缺少 config.json 时，读 [安装与配置](references/安装与配置.md)。SKILL_DIR 指本次加载技能的安装目录绝对路径；命令中替换为该路径。该目录的 config.json 可链接到独立的本机私有配置，不要复制其他人的配置、状态或登录信息。

群通知 key 由配置中 webhook_key_env 指定的环境变量提供，默认示例为 WECOM_WEBHOOK_KEY。配置只保存变量名，不保存 key 或完整 Webhook。不要打印或复述变量值；缺少变量时分别报告文档发布和通知状态。不得为测试环境变量而向真实群发送消息。

## 发布与更新

```text
python3 SKILL_DIR/shared_publisher.py --config SKILL_DIR/config.json publish --project api --tapd TAPD原链接 --file Markdown绝对路径 --kind 对接与测试说明 --execute
```

同一份正文涉及 Admin 时加 `--also-project admin`；预览时省略 `--execute`。使用 shared_publisher.py，不能换成会全量重写独立目录的旧入口。

- 使用配置中的固定共享目录，先读取再局部追加缺失链接，保留他人的内容。同一 TAPD 需求复用唯一入口、按项目分组；不自动读取或回写 TAPD。
- 更新自己登记的同一份源文件沿用原链接。文件改名、移动或接管他人的文档时先核实映射，不能据同名文件覆盖他人的正文。
- 图片先上传，Mermaid 先转 PNG；失败就报告，不能把代码块当流程图。未发布的本地 Markdown 参考只显示文件名，不递归发布。
- 正文全量更新会重建正文块，不能保证行内评论保留。有保留讨论要求时说明限制并选择局部修改。
- 首次发布和内容更新成功后，工具自动通知已配置且已授权的群，内容不变不发。不能在工具外重复通知，不能因通知失败重建文档；设置 key 本身不会发消息。
- 多个需求候选或远端写入结果不确定时停止核实；不猜测、不清空状态或盲目重试。通知 unknown/sending 时由用户在群里核实；明确未发送的最近事件才能按要求 notify-retry。
- 按配置排除的成员不查询、不同步，也不列为待处理。分别核实正文发布、通知送达和成员客户端访问。

## 阅读权限

link_access 缺省为 members（指定成员），corp_read 表示企业内持链接只读、企业外禁止通过链接加入。仅按用户已经确认的范围配置，不开放企业内编辑或公开访问；保留指定编辑者。应用创建的文档需通过接口设置权限，不能依靠客户端申请。

新建或更新本机管理的文档会自动应用该配置。用户要求迁移现有规则时：

```text
python3 SKILL_DIR/shared_publisher.py --config SKILL_DIR/config.json sync-rules --include-shared --execute
```

只作用于本机已登记文档；include-shared 明确包含接入的共享目录/需求入口，普通发布不修改其他作者的目录权限。迁移不改正文、不查通讯录、不发群通知，也不撤销单独授予的成员权限。企业内阅读不代表拥有共享目录编辑权；指定发布者编辑权需创建应用通过成员接口另行授予。

## 读取与维护

```text
python3 SKILL_DIR/shared_publisher.py --config SKILL_DIR/config.json read --url 企微文档链接
python3 SKILL_DIR/shared_publisher.py --config SKILL_DIR/config.json status
```

用户明确要求补齐已确认名单时才执行 sync-members。接口成功不能代替其他同事客户端验收。配置、发布状态、私密文件和环境变量值均不进入 Git、分享包或回复。
