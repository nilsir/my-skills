# AI Skills

[English](README.md) | 中文

这个仓库用于存放可复用的 AI skills。每个 skill 都放在一个顶级目录中，并且必须包含 `SKILL.md` 文件。

## 当前 Skills

| Skill | 运行时 | 用途 |
| --- | --- | --- |
| `claude-md-reviewer` | Claude Code | 使用评分细则审查并改进 `CLAUDE.md` 文件，输出问题和修改建议。 |
| `wecom-doc-publish` | Codex | 将 Markdown 发布到企微共享文档目录，关联 TAPD，并在首次发布或内容变化时通知群。 |

`运行时` 列标明 skill 面向哪个 agent 运行时。两者通用的 skill 标记为 `Claude Code / Codex`。

## 仓库结构

```text
.
├── README.md
├── README.zh-CN.md
├── LICENSE
├── .gitignore
├── wecom-doc-publish/
│   ├── SKILL.md
│   ├── config.example.json
│   └── references/
└── claude-md-reviewer/
    ├── SKILL.md
    └── references/
        ├── scoring-rubric.md
        ├── template.md
        └── example-review.md
```

## 下载单个 Skill

```bash
repo="git@github.com:nilsir/my-skills.git"
skill="claude-md-reviewer"
dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
# Codex（仅适用于标注 Codex 或 Claude Code / Codex 的 skill）：dest="${CODEX_HOME:-$HOME/.codex}/skills"
tmp="$(mktemp -d)"

git clone --depth 1 --filter=blob:none --sparse "$repo" "$tmp"
git -C "$tmp" sparse-checkout set "$skill"
mkdir -p "$dest"
cp -R "$tmp/$skill" "$dest/"
```

## 下载全部 Skills

```bash
repo="git@github.com:nilsir/my-skills.git"
dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
# Codex（仅适用于标注 Codex 或 Claude Code / Codex 的 skill）：dest="${CODEX_HOME:-$HOME/.codex}/skills"
tmp="$(mktemp -d)"

git clone --depth 1 "$repo" "$tmp"
mkdir -p "$dest"
find "$tmp" -mindepth 1 -maxdepth 1 -type d \
  -exec test -f "{}/SKILL.md" \; \
  -exec cp -R "{}" "$dest/" \;
```

## 更新已安装的 Skill

升级时遵循对应技能的安装说明。对于 wecom-doc-publish 这类保存本机状态的技能，保留配置、发布记录、私密文件和现有文档映射，不删除整个安装目录；只替换版本管理中的代码与参考文档，再核对配置示例的变化。

## 创建或更新 Skill

每个 skill 建议使用以下结构：

```text
skill-name/
├── SKILL.md
├── agents/
│   └── openai.yaml   # 仅 Codex 需要——标注 Claude Code / Codex 通用的 skill 才用
├── scripts/
├── references/
└── assets/
```

只有 `SKILL.md` 是必需的。只有当 `agents/`、`scripts/`、`references/`、`assets/` 能直接支撑该 skill 时才添加。`agents/openai.yaml` 是 Codex 专属适配文件——仅服务 Claude Code 的 skill 不需要它。保持 `SKILL.md` 简洁，把较长的参考资料放进 `references/`。

## 校验清单

发起 PR 之前请检查：

- 每个 skill 目录都有有效的 `SKILL.md`。
- YAML frontmatter 只包含必需的 `name` 和 `description` 字段，除非目标运行时明确支持更多字段。
- `SKILL.md` 提到的每个相对路径都真实存在。
- 如果 skill 附带辅助脚本，确认其可执行并至少运行验证过一次。
- 示例文件不包含密钥、凭证、隐私数据或一次性项目指令。

## Pull Request 流程

1. Fork 这个仓库。
2. 创建分支：

   ```bash
   git checkout -b feature/add-or-update-skill
   ```

3. 新增或更新 skill。
4. 执行上面的校验清单。
5. 提交修改：

   ```bash
   git add .
   git commit -m "feat: add or update skill"
   ```

6. 推送分支：

   ```bash
   git push origin feature/add-or-update-skill
   ```

7. 发起 PR，并说明：
   - 哪个 skill 发生变化。
   - 哪些用户请求应该触发它。
   - 已执行哪些校验。
   - 已知限制。

## License

基于 [MIT License](LICENSE) 发布。

## 企微文档发布

见[安装与配置](wecom-doc-publish/references/安装与配置.md)。群机器人 key 通过 `WECOM_WEBHOOK_KEY` 环境变量传入，仓库只保存变量名。实际密钥、本机配置和发布记录不入库。
