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
