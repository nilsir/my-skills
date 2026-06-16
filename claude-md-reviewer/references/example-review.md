# 示例：完整审查报告输出

> 此文件展示了一份标准审查报告的格式，供参考。

---

## 被审查的 CLAUDE.md（示例输入）

```markdown
# My Project

This is a web application built with React and TypeScript.

Please write clean code and follow best practices.

Make sure to handle errors properly.
```

---

## 生成的审查报告（示例输出）

---

## CLAUDE.md 审查报告

**总分：14 / 80（18%）** — 🔴 需重写

> 文件过于简单，缺少几乎所有必要信息。Claude 每次开始工作时都需要猜测命令、风格和约束，这会导致不一致的输出和重复的澄清。

---

### 各维度得分

| 维度 | 得分 | 状态 | 说明 |
|------|------|------|------|
| 简洁性 | 5/10 | ⚠️ | 内容严重缺失，简洁性按完整性 gate 封顶 5 分 |
| 命令完整性 | 0/10 | ❌ | 无任何命令 |
| 代码风格 | 1/10 | ❌ | "写好代码"不算代码规范 |
| 架构上下文 | 2/10 | ❌ | 只知道"React + TypeScript"，不知道具体结构 |
| 行为约束 | 0/10 | ❌ | 无任何行为约束原则 |
| 测试规范 | 0/10 | ❌ | 未提及测试 |
| 安全与规则 | 0/10 | ❌ | 无硬性规则 |
| 可读性 | 6/10 | 🟡 | 有标题但内容太少 |

---

### 发现的问题

**🔴 严重问题**（直接影响 Claude 工作效率）

1. **无命令**：Claude 不知道如何构建、测试或运行项目。每次都需要先探索 package.json，容易出错。
2. **无行为约束**：没有任何关于假设、简洁性或精准修改的指令，Claude 可能过度工程化或随意修改无关代码。
3. **无测试规范**：不知道测试框架、如何运行测试，Claude 可能写出不兼容的测试代码。

**🟠 需改进**

4. **代码规范过于笼统**：`"write clean code"` 无可操作性，应指定模块格式、命名约定、TypeScript 配置。
5. **架构信息缺失**：不知道状态管理方案、API 层、关键目录结构。

**🟡 建议优化**

6. **无安全规则**：建议添加"永不提交 API 密钥"等基本禁令。

---

### 优化方向预览

以下是建议的具体修改：

1. **新增命令章节**：
   ```markdown
   ## Commands
   - Build: `npm run build`
   - Dev: `npm run dev`
   - Test: `npm run test`
   - Lint: `npm run lint`
   ```

2. **替换笼统风格描述**，改为：
   ```markdown
   ## Code Style
   - Use TypeScript strict mode
   - ES modules (import/export), not CommonJS
   - Functional components with hooks only
   ```

3. **新增行为约束章节**（见 template.md 的 Behavior Constraints 部分）

4. **新增测试规范**（需用户确认使用的测试框架）

5. **新增安全规则**：
   ```markdown
   ## Rules
   - Never commit API keys or `.env` content
   ```

---

**您希望如何处理这些建议？**

- 输入 `全部应用` — 一次性应用所有修改
- 输入 `逐条确认` — 我会逐项询问您
- 输入具体编号（如 `1,3,5`）— 只应用指定修改
- 输入 `取消` — 仅供参考，不做修改

---

## 修改完成后的确认示例

---

✅ **修改已应用**

已对 CLAUDE.md 进行以下变更：
- 新增 `## Commands` 章节（+6 行）
- 将 "write clean code" 替换为具体规范（+8 行，-1 行）
- 新增 `## Behavior Constraints` 章节（+24 行）
- 新增 `## Rules` 章节（+4 行）

新总分估算：**约 58 / 80（73%）** — 🔴 需重写 → 🟡 需要改进

建议下一步：将文件提交到版本控制，并在实际使用中根据 Claude 的行为进一步微调。
