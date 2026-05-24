# sprite-gen

用 [Codex CLI](https://developers.openai.com/codex/cli) 內建 `image_gen`（gpt-image-2）自動生成 **pixel-art 角色 sprite 動畫與遊戲素材** — 逐格 PNG、橫向 sprite sheet、透明去背、動畫 GIF。

純文字描述即可從零生成角色（不需 reference 圖），也可附一張圖鎖定角色。**完全走 CLI，不需啟動任何 server。**

同時是一個 [Claude Code](https://claude.com/claude-code) **skill**（見 [`SKILL.md`](SKILL.md)）：把整個 repo 放到 `~/.claude/skills/sprite-gen/`，Claude 就會在你要求生 sprite 時自動使用。

## 需求

- [Codex CLI](https://developers.openai.com/codex/cli) `>=0.128`（內建 `image_gen`），且已 `codex login`
- Python **3.10+**
- macOS / Linux

## 安裝

### 方式 A — 裝成「當前 repo」的 Claude Code skill（推薦）

在你**正在開發的專案根目錄**執行，把 sprite-gen clone 進該專案的 `.claude/skills/`，Claude 在這個 repo 工作時就會自動有 `sprite-gen` skill：

```bash
# 在你的專案根目錄下
git clone https://github.com/gpwork4u/sprite-gen.git .claude/skills/sprite-gen
( cd .claude/skills/sprite-gen && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt )
```

之後直接跟 Claude 說「生一隻 XX 角色的 walk/idle」即可；生成的素材會落在**當前專案目錄**的 `.sprites/`。

> 建議把 `.claude/skills/sprite-gen/.venv/` 與 `.sprites/` 加進專案的 `.gitignore`；
> 若不想連 skill 原始碼一起 commit 進你的 repo，把整個 `.claude/skills/sprite-gen/` 也 ignore 掉即可。

裝成**全使用者通用**（所有專案都可用）就改 clone 到 `~/.claude/skills/sprite-gen`。

### 方式 B — 獨立 CLI 使用（不當 skill）

```bash
git clone https://github.com/gpwork4u/sprite-gen.git
cd sprite-gen
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 用法

1. 寫一份 pack spec（見 [`examples/pack.yaml`](examples/pack.yaml)）：

```yaml
output_root: .sprites
defaults: { template: sidescroller_character, view: side, frames_per_image: 4, frame_size_px: 384, duration_ms: 120, qc_max_retries: 4 }
characters:
  - id: knight
    design:
      - "young knight, short brown hair, steel plate over a blue tunic"
      - "round wooden shield, short sword, chibi proportions, chunky pixel outlines"
    actions:
      - { action: idle, frames: 4 }
      - { action: walk, frames: 6 }
      - { action: attack, frames: 5, notes: "horizontal sword swing" }
assets:
  - id: potions
    name: potion
    design: ["glass potion bottles, cork stopper, glossy highlight"]
    variations: ["red health potion", "blue mana potion", "green poison vial"]
```

2. 先 dry-run 看 prompt（不花 token），再真跑：

```bash
.venv/bin/python -m sprite_gen pack examples/pack.yaml --dry-run   # 只產 prompt
.venv/bin/python -m sprite_gen pack examples/pack.yaml             # 真跑
```

## 輸出

```
.sprites/
├── knight/
│   ├── idle/   final/{frames,transparent-frames,strip.png,animation.gif,transparent.gif,transparent-strip.png}
│   ├── walk/   …（>4 格會有 chunk-01 / chunk-02 子目錄）
│   └── attack/ …
└── potions/    final/…（一個 panel 一種變體，逐個透明去背）
```

每個動作都有：逐格 PNG（帶背景 + 去背）、橫向 sprite sheet、預覽 GIF。

## 運作原理

- **Sprite sheet → 切格**：codex 一次生整條多 frame strip（避免逐張對不齊），Python 端切等格組 GIF。
- **文字一致性**：無 reference 時，`design` 文字 + 嚴格 layout 約束（等寬 panel、固定 bbox、≤40% 填充）讓同一角色在每格保持一致。
- **QC 自動重試**：切格後檢查 clipping / 大小漂移，失敗帶錯誤訊息重生（預設最多 4 次）。
- **長動畫 chunk 串接**：>4 格拆成多次 codex call，用前一段當 reference 維持連續。
- **去背**：背景固定 chroma green `#00B140`，corner-sample + edge BFS 清 halo（不依賴 ML）。
- **大小穩定**：動畫共用單一縮放比例（身體大小固定、只有姿勢變化）；靜態素材逐格正規化（每個變體一致大小）。

## 子指令

```bash
python -m sprite_gen pack <spec>.yaml [--dry-run] [--output-root DIR] [--max-workers N]
python -m sprite_gen process <raw-sheet>.png --cols N -o out/ [--static] [--chroma-key]
```

`process` 可重切 / 重去背已生成的 raw 圖而不重呼叫 codex。

## Templates

| template | 用途 | 需要 reference 圖 |
|---|---|---|
| `sidescroller_character` | 2D 橫向捲軸角色動畫（side/front/back/3-4/topdown） | 否（純文字即可） |
| `static_asset` | 靜態素材／道具／圖示，一個 panel 一個變體 | 否 |
| `hd2d_anime_32frame` | 保留 reference 構圖的 HD-2D anime strip | 是 |

## Credits

- 圖像生成由 [Codex CLI](https://developers.openai.com/codex/cli) + `gpt-image-2` 完成

## License

MIT — 見 [`LICENSE`](LICENSE)。
