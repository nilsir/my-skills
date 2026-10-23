# AI Skills

English | [中文](README.zh-CN.md)

This repository contains reusable AI skills. Each skill lives in its own top-level directory and must include a `SKILL.md` file.

## Available Skills

| Skill | Runtime | Purpose |
| --- | --- | --- |
| `claude-md-reviewer` | Claude Code | Reviews and improves `CLAUDE.md` files with a scoring rubric and suggested edits. |
| `wecom-doc-publish` | Codex | Publishes Markdown to shared WeCom documents, links TAPD requirements, and notifies a group on content changes. |

The `Runtime` column states which agent runtime a skill targets. Skills that work in both are marked `Claude Code / Codex`.

## Repository Layout

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

## Install One Skill

```bash
repo="git@github.com:nilsir/my-skills.git"
skill="claude-md-reviewer"
dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
# Codex (only for skills marked Codex or Claude Code / Codex): dest="${CODEX_HOME:-$HOME/.codex}/skills"
tmp="$(mktemp -d)"

git clone --depth 1 --filter=blob:none --sparse "$repo" "$tmp"
git -C "$tmp" sparse-checkout set "$skill"
mkdir -p "$dest"
cp -R "$tmp/$skill" "$dest/"
```

## Install All Skills

```bash
repo="git@github.com:nilsir/my-skills.git"
dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
# Codex (only for skills marked Codex or Claude Code / Codex): dest="${CODEX_HOME:-$HOME/.codex}/skills"
tmp="$(mktemp -d)"

git clone --depth 1 "$repo" "$tmp"
mkdir -p "$dest"
find "$tmp" -mindepth 1 -maxdepth 1 -type d \
  -exec test -f "{}/SKILL.md" \; \
  -exec cp -R "{}" "$dest/" \;
```

## Update an Installed Skill

Follow the skill’s installation guide when upgrading. Preserve local configuration, publication state, private files and existing document mappings for stateful skills such as `wecom-doc-publish`; do not delete the installed directory. Replace versioned code and references only, then review changes to the configuration example.

## Create or Update a Skill

Use this structure for each skill:

```text
skill-name/
├── SKILL.md
├── agents/
│   └── openai.yaml   # Codex only — required for Claude Code / Codex skills
├── scripts/
├── references/
└── assets/
```

Only `SKILL.md` is required. Add `agents/`, `scripts/`, `references/`, and `assets/` only when they directly support the skill. `agents/openai.yaml` is a Codex-specific adapter — Claude Code skills do not need it. Keep `SKILL.md` concise and put large reference material in `references/`.

## Validation Checklist

Before opening a pull request:

- Confirm each skill folder has a valid `SKILL.md`.
- Confirm the YAML frontmatter includes only the required `name` and `description` fields unless the target runtime explicitly supports more.
- Confirm every relative path mentioned by `SKILL.md` exists.
- If the skill ships helper scripts, confirm they are executable and have been run at least once.
- Confirm examples do not include secrets, credentials, private data, or project-specific one-off instructions.

## Pull Requests

1. Fork the repository.
2. Create a branch:

   ```bash
   git checkout -b feature/add-or-update-skill
   ```

3. Add or update the skill.
4. Run the validation checklist above.
5. Commit your changes:

   ```bash
   git add .
   git commit -m "feat: add or update skill"
   ```

6. Push your branch:

   ```bash
   git push origin feature/add-or-update-skill
   ```

7. Open a pull request and include:
   - Which skill changed.
   - What user requests should trigger it.
   - What validation you ran.
   - Any known limitations.

## License

Released under the [MIT License](LICENSE).

## WeCom Document Publishing

See [installation and configuration](wecom-doc-publish/references/安装与配置.md). The group robot key is read from the `WECOM_WEBHOOK_KEY` environment variable; only its variable name is versioned. Local configuration, publication state and secrets are ignored.
