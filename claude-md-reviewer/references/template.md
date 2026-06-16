# CLAUDE.md 优质模板

> 此模板集成了 Claude Code 官方最佳实践 + 行为约束四原则。
> 使用时删除所有注释行（以 `> ` 开头的行），替换 `[占位符]` 内容。

---

```markdown
# [项目名称]

## Commands

> 列出所有常用命令。让 Claude 不用猜。
- Build: `[构建命令，如 npm run build]`
- Dev: `[开发服务器，如 npm run dev]`
- Test (single file): `[如 npx vitest run src/foo.test.ts]`
- Test (all): `[如 npm run test]`
- Lint: `[如 npm run lint]`
- Type check: `[如 npx tsc --noEmit]`

## Code Style

> 明确规范，不要让 Claude 猜风格。
- Module format: [ESM (`import/export`) / CommonJS (`require`)]
- Language: [TypeScript strict mode / JavaScript / Python 3.11+]
- Components: [函数式组件 + hooks / 类组件]
- Naming: [camelCase for vars/functions, PascalCase for components/classes]
- Imports: [destructure preferred / default imports only]
- [其他框架特定规范]

## Architecture

> 只列关键信息，帮助 Claude 理解代码组织。
- State management: [Zustand / Redux / Context — see src/stores/]
- API layer: [React Query / SWR / axios — see src/hooks/]
- Routing: [React Router v6 / Next.js App Router]
- Key directories:
  - `src/components/` — 可复用 UI 组件
  - `src/features/` — 按功能分组的业务逻辑
  - `src/utils/` — 纯函数工具

## Testing

> 测试框架和运行方式。
- Framework: [Vitest + React Testing Library / Jest / pytest]
- Run single test: `[命令]`
- New components require a test file
- [覆盖率要求，如 Aim for 80%+ on new utilities]

## Rules

> 硬性规则。Claude 必须遵守，不能以"更好的实现"为由绕过。

### Never Do
- Never commit API keys, tokens, or `.env` content
- Never modify production database configs directly
- Never upgrade major dependency versions without asking
- [项目特定禁止项]

### Behavior Constraints

**Think before coding**
- When a request is ambiguous, ask — don't assume
- Present multiple interpretations when requirements are unclear
- State your assumptions explicitly before starting
- Push back if there's a simpler approach

**Prefer simplicity**
- Use the minimum code to solve the problem
- Don't add features, configs, or abstractions that weren't asked for
- If 50 lines can replace 200 lines, rewrite it
- Ask yourself: would a senior engineer call this over-engineered?

**Surgical edits**
- Only touch code directly related to the request
- Don't "improve" adjacent code, formatting, or comments
- Match existing style, even if you'd prefer otherwise
- If you spot unrelated dead code, mention it — don't delete it

**Goal-driven execution**
- Define success criteria before implementing
- Run tests after changes; iterate until they pass
- Show evidence of success (test output, build log) — don't just assert it worked
- For multi-step tasks, outline a plan and verify each step
```

---

## 说明

### 什么内容不应该放在 CLAUDE.md

- **深度架构文档**：移至 `.claude/skills/architecture/` skill
- **一次性任务指令**：每次 prompt 时给出即可
- **Claude 本来就会的事**：如"写代码要认真"、"注意边界情况"
- **详细的 API 参考**：移至 skills 或 references/
- **只适用于部分任务的规则**：移至专用 skill

### 长度建议

| 项目规模 | 建议行数 |
|----------|----------|
| 个人项目 | 30–60 行 |
| 中型团队项目 | 60–120 行 |
| 大型多模块项目 | 100–180 行 |
| 上限（任何项目） | 200 行 |

超过 200 行通常意味着某些内容应该移至 skills。
