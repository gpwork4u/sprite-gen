---
name: sprite-gen
description: 用 codex CLI 內建 image_gen 自動生成 pixel-art 角色 sprite 動畫與遊戲素材（逐格 + 去背 + GIF），不需啟動任何 server。當使用者要求「生成角色 sprite / 像素動畫 / 遊戲素材 / 道具圖示 / walk/run/idle/attack 動畫 / sprite sheet」，或提到 "sprite", "pixel art", "sprite sheet", "角色動畫", "像素素材", "去背逐格" 時使用此 skill。
---

# sprite-gen

純文字（或選用 reference 圖）驅動的 pixel-art sprite 產生器。底層呼叫 **codex CLI 的 `image_gen`**（gpt-image-2）生成 sprite sheet，再用 Python 切格 / 去背 / 組 GIF。**不依賴啟動任何 web server** — 全部走 CLI。

## 何時用

- 使用者要一整套角色動畫（idle / walk / run / jump / attack / hurt …），側視 2D 橫向捲軸風格。
- 使用者要靜態遊戲素材 / 道具 / 圖示（武器、藥水、寶箱…），透明去背。
- 使用者「沒有原圖、想用文字描述直接生成」，或「有一張角色圖、要做成動畫」。

## 前置需求（先檢查）

1. **codex CLI** `>=0.128` 已安裝且登入：
   ```bash
   codex --version          # 應 >= 0.128
   codex login              # 若尚未登入（互動式，請使用者自行執行）
   ```
   若使用者尚未登入，請他在對話框輸入 `! codex login` 自行完成。
2. **Python 3.10+** 與相依套件（numpy / Pillow / PyYAML）。建議用此 skill 目錄下的 venv：
   ```bash
   cd "<skill-dir>"                       # 此 SKILL.md 所在目錄
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
   後續所有指令都用 `.venv/bin/python`。

## 自動撰寫原則（重要）

使用者通常只給**一句粗略描述**（例如「一個森林精靈弓箭手」）。你的工作是**自動把它補完成一份完整 pack YAML 並直接生圖**，不要反問一堆問題。具體：

- **把一句話展開成 4–6 行具體 `design`**：補上髮型/髮色、服裝、配件/武器、體型比例、畫風（chunky pixel outlines 等）。描述要具體可重現——這是無 reference 時角色的唯一依據。
- **自動挑動作組合**：使用者沒指定就給一套常見的（`idle` + `walk` + `attack`）；有指定就照他的。格數用預設（idle/hurt 3–4、walk/run 6、attack 5–6）。
- **自動挑 `view`**：橫向捲軸/平台遊戲角色用 `side`；頭像/正面立繪用 `front`；道具用 `static_asset`。
- **只在描述真的不足以推斷時才問**（例如完全沒講是角色還是物件）。其餘用合理預設並在回覆裡說明你選了什麼。
- 寫完 YAML **先快速 dry-run 自查 prompt**，沒問題就**直接真跑**，最後用讀圖工具看 `transparent-strip.png` 確認再交 GIF 給使用者。

## 工作流程（給 Claude 的步驟）

1. **把使用者需求轉成一份 pack YAML**。每個角色一個 `id` + `design`（一行一個外觀特徵）+ `actions`（要哪些動作、幾格）。靜態素材放 `assets`。範本見 `examples/pack.yaml`。重點：
   - `design` 用具體、可重現的文字（髮色、服裝、配件、比例、畫風）。**無 reference 圖時，這就是角色的唯一依據**，model 會據此生成並在各格保持一致。
   - 動畫格數 `frames`：idle/hurt 用 3–4、walk/run 用 6、attack 用 5–6。>4 格會自動切 chunk 串接。
   - `view`: 橫向捲軸用 `side`（嚴格朝右 profile）；也支援 `front` / `back` / `3/4` / `topdown`。

2. **先 dry-run**，把所有 prompt 寫出來給使用者（或自己）審，不花 token：
   ```bash
   .venv/bin/python -m sprite_gen pack <spec>.yaml --dry-run
   ```
   prompt 會寫到 `<output_root>/<id>/<action>/[chunk-NN/]prompt.txt`。

3. **小量真跑驗證**整條鏈再放大（codex 每張圖約 10–60s，計入 codex 額度）。建議先留 1 個角色的 idle + 1 個 walk：
   ```bash
   .venv/bin/python -m sprite_gen pack <spec>.yaml
   ```
   每張圖會跑 QC：切格後自動檢查 clipping / 大小漂移，失敗會帶著錯誤訊息重試（預設最多 4 次）。

4. **檢視成品再回報**。輸出在 `<output_root>/<id>/<action>/final/`：
   - `frames/frame-NNN.png`（逐格，帶背景）、`transparent-frames/frame-NNN.png`（**逐格去背**）
   - `strip.png` / `transparent-strip.png`（橫向拼接 sheet）
   - `animation.gif` / `transparent.gif`（預覽動畫）
   用讀圖工具看 `transparent-strip.png` 確認角色一致、去背乾淨、動作正確，再把 GIF 交給使用者。

## 重要 tuning 與注意

- **成本**：每張 image 一次 codex session。第一次只開最小規模（1 角色、少數動作）確認 OK 再 scale。長時間真跑請放背景並輪詢。
- **嚴格側視**：`view: side` 已強制 90° 朝右 profile（禁止 3/4 / 正面漂移）。若仍漂移，重生即可。
- **大小穩定**：動畫用「全批共用縮放」避免逐格忽大忽小；靜態素材用「逐格正規化」讓每個變體一致大小（已自動依 template 切換）。
- **去背**：背景固定用 chroma green `#00B140`，post-process 自動 key 掉。角色若含相近綠色，prompt 已要求偏移色相。
- **重新後處理**：已生成的 raw 圖存在各 `attempt-NN/raw-from-codex.png`，可用 `process` 子指令重切 / 重去背而**不重呼叫 codex**：
  ```bash
  .venv/bin/python -m sprite_gen process <raw>.png --cols 4 -o out/
  ```

## 範例 pack 片段

```yaml
output_root: .sprites
defaults: { template: sidescroller_character, view: side, frames_per_image: 4, frame_size_px: 384, duration_ms: 120, qc_max_retries: 4, max_workers: 2 }
characters:
  - id: knight
    design:
      - "young knight, short brown hair, steel plate over a blue tunic"
      - "round wooden shield, short sword, chibi 3-head-tall, chunky pixel outlines"
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

完整可執行範本：`examples/pack.yaml`。

## 使用範例：一句描述 → 自動生圖

> **使用者**：「幫我做一個森林精靈弓箭手，要待機、走路、射箭三個動作。」

Claude（用此 skill）應**自動**做完以下事，過程中不反問：

**1. 把粗略描述展開並寫出 `/tmp/elf.yaml`：**

```yaml
output_root: .sprites
defaults: { template: sidescroller_character, view: side, frames_per_image: 4, frame_size_px: 384, duration_ms: 120, qc_max_retries: 4, max_workers: 2 }
characters:
  - id: elf-archer
    design:
      - "lithe female forest elf, long pointed ears, braided emerald-green hair"
      - "hooded leather tunic in mossy green and brown, fingerless gloves"
      - "carved wooden longbow, quiver of arrows on the back"
      - "chibi proportions, roughly 3 heads tall, chunky readable pixel-art outlines"
    actions:
      - { action: idle,   frames: 4, notes: "calm breathing, bow held loosely at side" }
      - { action: walk,   frames: 6, notes: "light forward steps, bow in hand" }
      - { action: attack, frames: 6, notes: "draw arrow, aim, release, recover" }
```

**2. dry-run 自查 → 直接真跑：**

```bash
.venv/bin/python -m sprite_gen pack /tmp/elf.yaml --dry-run   # 快速確認 prompt
.venv/bin/python -m sprite_gen pack /tmp/elf.yaml             # 真跑（背景執行，輪詢）
```

**3. 檢視 `transparent-strip.png` 確認角色一致/去背乾淨，把 GIF 交給使用者：**

```
.sprites/elf-archer/idle/final/transparent.gif
.sprites/elf-archer/walk/final/transparent.gif
.sprites/elf-archer/attack/final/transparent.gif
```

並回報：「幫你補了精靈弓箭手的外觀細節（綠髮辮、苔綠皮甲、木長弓+箭袋），生了 idle/walk/attack 三套側視動畫，逐格去背都在 `.sprites/elf-archer/`。」

### 更多觸發範例

| 使用者說 | skill 自動做 |
|---|---|
| 「生一隻紅色小恐龍的走路動畫」 | 1 角色 `dino`，`design` 補紅色小恐龍細節，`actions: [walk]`（frames 6） |
| 「我要一套 RPG 藥水圖示」 | `assets` 一個 `static_asset`，`variations` 補紅/藍/綠/金藥水 |
| 「賽博龐克女駭客，待機+攻擊」 | 1 角色，`design` 補霓虹髮色/科技外套/數據手套，`actions: [idle, attack]` |
| 「一把魔法劍的圖示」 | `assets` 單一 `static_asset`（無 variations → 單張置中精靈圖） |
