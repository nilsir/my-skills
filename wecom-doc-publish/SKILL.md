---
name: wecom-doc-publish
description: Use when publishing or updating local Markdown as WeCom documents for API, Admin or Mall, associating TAPD requirements, notifying a configured WeCom group after publication changes, or reading shared WeCom document links. 中文触发词：发布到企微、测试交接文档、前端联调文档、更新对接文档、发布测试说明、读取企业微信文档。
---

# 企微文档发布

本地 Markdown 是正文来源。用户只要求写文档时先保存到本地；明确要求发布或更新后执行同步，并在发布成功后通知已配置且已授权的群，除非用户明确要求不通知。项目、文件、文档类型和 TAPD 能从上下文确定时，直接执行，不重复询问发布或群通知许可。

## 安装与配置

首次使用或缺少 config.json 时，读 [安装与配置](references/安装与配置.md)。SKILL_DIR 指本次加载技能的安装目录绝对路径；命令中替换为该路径。该目录的 config.json 可链接到独立的本机私有配置，不要复制其他人的配置、状态或登录信息。

群通知 key 由配置中 webhook_key_env 指定的环境变量提供，默认示例为 WECOM_WEBHOOK_KEY。配置只保存变量名，不保存 key 或完整 Webhook。不要打印或复述变量值；缺少变量时分别报告文档发布和通知状态。不得为测试环境变量而向真实群发送消息。

## 发布与更新

```text
python3 SKILL_DIR/shared_publisher.py --config SKILL_DIR/config.json publish --project api --tapd TAPD原链接 --file Markdown绝对路径 --kind 对接与测试说明 --execute
```

同一份正文涉及 Admin 时加 `--also-project admin`；预览时省略 `--execute`。使用 shared_publisher.py，不能换成会全量重写独立目录的旧入口。

### 根据文档用途自动选择通知模板

用户只说“发布到企微”时，也应先读本地正文，结合标题、主要读者和用途自行选模板，不要求用户再次指定类型：

| 主要用途 | 使用模板 | 通知分组 |
| --- | --- | --- |
| 交给测试执行用例、验证功能与回归，交接已验证结果及待测项 | [测试交接通知模板](references/测试交接通知模板.md) | 交付信息、上线依赖、测试重点、上线顺序 |
| 交给前端按接口契约接入页面，说明参数/字段/状态、环境及接入步骤 | [前端联调通知模板](references/前端联调通知模板.md) | 联调信息、接口与依赖、联调重点、联调顺序 |
| 设计、排查或其他普通说明 | 既有简短通知 | 项目、正文与相关目录链接 |

前端联调文档含测试用例时仍按前端主要用途选择；面向测试的验收文档引用接口时仍选测试交接模板。混合文档按主要用途选一套并补充必要的另一类重点，同一份正文只发一条通知，不机械根据文件名或“测试”一词判断。

按所选模板，从本地正文、实际分支及已核实环境填好本次通知Markdown，不编造接口、字段、部署、自验或上线结论；未知信息写“待确认”。仅保留一个 `{document_url}` 链接占位符，在发布命令加 `--notification-file 通知绝对路径`，正文及目录成功后由同一持久事件发送，不能先发旧格式再补发。没有真实TAPD链接时，不伪造需求关联；复用现有项目目录和底层发布能力，通知同样通过 `group_notify.markdown_payload()` 与持久事件发送。通知内容不超过4096 UTF-8字节，过长时保留摘要、细节指向正文。

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
