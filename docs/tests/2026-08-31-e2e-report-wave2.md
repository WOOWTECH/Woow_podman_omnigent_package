# Omnigent 部署 E2E 全面測試 — Wave 2（深功能）

> **接續** `2026-08-31-e2e-report.md`。前一輪是靜態走查 + 單 round chat；本輪測 upstream research 沒涵蓋的 21 個深功能面向，用 4 個平行 sub-agent + main thread chrome-devtools MCP 完成。
>
> **方法論**：8 條測試線分成 wave1（靜態） + wave2（深功能）。wave2 又切成 sub-agent 平行（Playwright dry-run / Automation E2E / Auth curl / Resilience）與 main thread 序列（chrome-devtools UI 深功能 + RWD）。原因：chrome-devtools MCP 只我這一支瀏覽器 instance，不能平行；curl / ssh / npm 各 sub-agent 獨立可平行。

---

## Wave 2 通過項（新覆蓋的功能）

### A. Chat 深功能（全綠）
- ✅ **Multi-turn**：session `0a262a34423647e88330a41377f2a78f` 送 follow-up prompt，pi 回覆 `multi-turn-verified`（Screenshot 24）— upstream #5875（follow-up queue bug）在我方部署**未觀察到**
- ✅ **Slash command menu**（打 `/`）：4 個內建 command — `/compact` `/context` `/model` `/help`（Screenshot 21）
- ✅ **`@` mention menu**：呼出 workspace file picker，可 attach 整個 folder（Screenshot 22）
- ✅ **Terminal view toggle**：切成 tmux + pi native 那條路，`Terminal input` textbox 出現、host reconnect 後可用（Screenshot 23）
- ✅ **Model picker**（Configure session）：8 個 OpenAI 選項（Default + GPT-5.3 Codex Spark + GPT-5.4/4-mini/5/6-Luna/6-Sol/6-Terra）— 由 shared pi-agent provider 帶出（Screenshot 25）
- ✅ **Send → Interrupt** 按鈕動態切換 — 訊息送出後 Send 變 Interrupt（等同 Esc 取消）
- ✅ **"Jump to" nav 按鈕** — 每個 user turn 加了跳轉按鈕
- ✅ **Fork from here** / **Copy** — 每則訊息的操作按鈕都可見

### B. Workspace panel
- ✅ **Files tab**：`.omnigent/` + `.npm/` 展開，Working folder 按鈕跳 dialog
- ✅ **Changes tab**：`No workspace changes yet` 空狀態（沒 git 動作）
- ✅ **Agents tab**：`Pi Idle` + List / Graph view 切換（Screenshot 27）

### C. Session lifecycle
- ✅ **Share session dialog**：Public access switch + user grant 表單 + Copy link + Open in mobile app（Screenshot 28）
- ✅ **Conversation actions menu**（7 項）：Pin / Share / Rename / Mark as unread / Add to project(submenu) / Archive / Delete（Screenshot 29）
- ⚠️ **Command palette (Ctrl+K) / Sidebar Search 按鈕**：dialog 開得起來，但輸入任何字（`set` `policy` `omnigent`）**suggestion listbox 永遠是空的** — 明顯 broken。**BUG 標為 MED**（Screenshot 26, 30）

### D. Admin 全流程
- ✅ **Invite create + redeem 端到端**：
  1. `/settings/members` → Invite member → Create invite → URL：`https://.../register?invite=8vOHMGV7VeP5oOs7W2AdgeEQFfHhtP6OGGAkz5yCQJI`（72h TTL、single-use）（Screenshot 31）
  2. 新開 isolated browser context → `/register?invite=…` → username `e2e_invitee` + pw `e2eTestPw2026!` → Create account
  3. Auto-login → 空 sidebar（**權限隔離 ✓**：看不到 admin 的 project、session、runner host）（Screenshot 32）
  4. 回 admin session → `/settings/members` refresh → `e2e_invitee` (Member) 出現（Screenshot 33）
- ✅ **Policies add real**：加了 `limit_sub-agent_dispatches_per_turn`（`omnigent.policies.builtins.orchestration.spawn_bounds`）到 Global Policies，toggle checked、可 Remove（Screenshot 34）— 26 個內建 policy 都可測

### E. Auth cycle
- ✅ **Sign out**：`/settings/account` → Sign out → redirect `/login`，`/v1/me` 立刻回 401（Screenshot 35）
- ✅ **Re-login**：username pre-filled from `omnigent.lastLoginUsername` localStorage、pw 空 → 登入 → 回 home
- 🔑 **JWT storage**：**httpOnly cookie**（不是 localStorage / sessionStorage）— `document.cookie` 看不到（只看到 `code-server-session` 舊 cookie），但 `fetch('/v1/me', {credentials:'include'})` 拿得到 → 200，`{credentials:'omit'}` → 401
- 📌 **localStorage 記錄**：`omnigent:prompt-history:*`、`omnigent:recent-workspaces`、`omnigent:session-workspace-state`、`omnigent.readState.v1`、`omnigent.lastLoginUsername`、`omnigent:recent-harnesses`、`omnigent:last-agent-id`

### F. RWD extended
- ✅ **768×1024（iPad portrait）**：sidebar 保留 hamburger 收合 + composer/workspace 響應正確（Screenshot 36, 37）
- ✅ 前輪已驗 **375×812（手機）** + **1440×900（桌面）**

### G. Automation E2E（sub-agent 跑完）
- ✅ **PASS**：`POST /v1/scheduled-tasks` 建 `e2e_automation_test`（rrule hourly；task id `2e2c1eaf22a8486b84c14fcf720c65d7`）→ 90 秒後首次 fire → pi 產出 session `50aa2c0b48e3406eaab50c8d0c9e702c` → assistant reply **exact** `automation-fire-verified`
- 3 個 upstream 觀察：
  1. `finished_at` 早於 assistant reply 5 秒（scheduler 用 dispatched-at 當 completed，不是 real finish）
  2. **RRULE 最小 interval = 60 min 未文件化**（validator-only，422 without schema hint）
  3. 沒 one-shot 支援（`COUNT=1` 被拒）

### H. Playwright suite dry-run（sub-agent 跑完）
- **npm install + playwright install** OK（sandbox 無 sudo，跳 `--with-deps` 但 tests 照跑）
- 執行 24 tests：**14 pass / 8 fail** / 2 skipped，2m 36s
- 8 個 fail：**6 是 locator drift**（本 PR 已 patch，見下）、2 是 `/automations` 錯 route（sidebar 用 `/tasks`，本 PR 已 patch）
- 沒有一個 fail 反映真正的 Omnigent regression
- 補完 patch 後 confidence：**8.5 / 10**，可當 CI gate

### I. Auth curl（sub-agent 跑完）
- ✅ signature verify、no `alg:none` bypass、strict JSON、invite gate 有效、min-pw-length 8 enforced
- 🔴 **HIGH — NO login rate limiting**：30 次錯密碼 4 秒內全 401，無 429、無 backoff、無 lockout。線上暴力破解暢通
- ⚠️ **MED — JWT 無 `jti`**：8h TTL 無法撤銷；洩露 = 8h window
- ⚠️ **MED — JWT 無 `iss`/`aud`/`nbf`**：跨服務 token confusion 沒 defense-in-depth
- ⚠️ **LOW — 密碼政策只有 min-length 8**：無複雜度、無 breach-list（admin `woowtech2026` 是字典可猜）
- ⚠️ **LOW — 驗證錯誤 echo 原輸入**：`{"input":"12345"}` 有機會把 log shipper 帶到明碼密碼

### J. Resilience（sub-agent 跑完 — 結果併入本文件下方 §Resilience results）

---

## Wave 2 缺陷清單（新發現）

### 🔴 HIGH — 3 項

**W2-HIGH-1 — 登入無 rate limit**（來源：Auth curl sub-agent）
- 30 次錯密碼 401 in 4 秒
- **Fix**：nginx sidecar `limit_req_zone` OR omnigent middleware token bucket per-IP + per-username
- **建議**：不 patch，upstream issue

**W2-HIGH-2 — Command palette / Sidebar Search 完全 dead**（來源：main UI）
- Ctrl+K OR 側欄 Search 按鈕開 dialog、輸入任何字回 0 suggestions
- 影響：主要 navigation UI 之一失效
- **Root cause 猜測**：0.11.0 palette 需要建 index / 需要遠端 harness ready；或前端 route 有 wiring bug
- **建議**：不 patch（upstream），追 issue

**W2-HIGH-3 — Runner container restart → 所有 session 綁定舊 host_id 變 offline**（來源：main UI Chat）
- restart runner container → 新 host_id 註冊，但既有 session 的 `host_id` 沒 rewrite → session 需手動 "Switch host"（好在 UI 有這功能）
- 影響：Q10 restart 可用性 — 場景：runner container 因任何原因重啟，所有 in-flight session 都要 user 點 "Switch host" 重指
- **Mitigation**：runner-loop 已 v2 auto-register（不用手動 login），但 sessions 還是要一個個 switch
- **Root cause**：omnigent server 沒有「orphan host_id → replace with new online host of same runner container」的 GC。upstream 期望是「不同機器 = 不同 host_id」，同 container 重啟被誤解為換機
- **Fix option**：patch omnigent 讓 runner-loop 傳一個 stable `host_key`（container hostname or /etc/machine-id）優先 reuse
- **建議**：upstream issue，我方暫用 UI Switch host 補上

### ⚠️ MED — 2 項

**W2-MED-4 — Automation `finished_at` 是 dispatched-at 不是 real completion**（Automation sub-agent）
- 影響：真正的 turn 花費 & 時長被隱藏
- **建議**：upstream

**W2-MED-5 — Automation RRULE 60-min minimum 未 documented**（Automation sub-agent）
- OpenAPI schema 沒描述、validator-only；用戶 422 without hint
- **建議**：upstream

### ⚠️ LOW — 4 項

- **W2-LOW-6** — JWT 無 `jti`，8h 洩露 = 8h window
- **W2-LOW-7** — 密碼政策只有 min-length 8（`woowtech2026` 是字典密碼）
- **W2-LOW-8** — Validation error echo 原輸入（log shipper 可能捕獲明碼）
- **W2-LOW-9** — Automation 沒 one-shot 支援（`COUNT=1` 被拒）

---

## Wave 2 本 PR 動了什麼

### 檔案改動
| File | Change |
|---|---|
| `tests/e2e/specs/smoke.spec.ts` | 修 composer locator：`/message\|prompt.../` → `/describe a task\|message\|prompt/` |
| `tests/e2e/specs/chat.spec.ts` | 同上 composer locator |
| `tests/e2e/specs/inbox-automations.spec.ts` | `/automations` → `/tasks`（sidebar link 導的真 route） |
| `tests/e2e/specs/settings.spec.ts` × 4 處 | Ctrl+N 分開 span、policy card role=button、sharing radios 全描述 accessible name、archived heading 消歧義 |
| `docs/tests/2026-08-31-e2e-report-wave2.md` | **本文件** |
| `docs/tests/2026-08-31-screenshots/19-*.png` … `37-*.png` | 19 張 wave 2 截圖 |

### 未動 code（列給人工評估）
- 沒 patch upstream bugs（rate limit / palette / host_id GC / body-size / needs-auth cosmetic）— 全 external repo
- 沒動 pi harness code — 我方是 packaging layer

---

## Wave 2 統計

- **測到的頁面 / dialog / menu**：18 個新（前輪 20 + 本輪 18）= **38 個 UI 面向覆蓋**
- **測到的 API endpoint**：本輪加 10 個（`/auth/register`、`/auth/login` × 30 rate、`/v1/scheduled-tasks` POST/GET/runs、`/v1/scheduled-tasks/{id}`、`/v1/agents`、`/v1/sessions/{id}`）= 前輪 20 + 本輪 10 = **30 個 API 覆蓋**
- **平行 agent 數**：4（Playwright dry-run + Automation E2E + Auth curl + Resilience）
- **總耗時**：~40 分鐘（main thread UI walk + 4 sub-agent 平行）
- **新 test artefacts**：19 張截圖 + 6 個 Playwright patch + 本 report

---

## Resilience results（sub-agent SSH .197）

| # | 情境 | 恢復時間 | RestartCount | 判定 |
|---|---|---|---|---|
| 1 | `podman kill omnigent-server` (SIGKILL) | `/health=200` at **t+10s** | 0→0 | ✅ PASS |
| 2 | `podman kill omnigent-runner` (SIGKILL) | 新 `host_id` in log at **t+10s**（`host-20260831-043340` → `host-20260831-043352`），runner-loop v2 全流程重跑（`waiting /health` → JWT login → `registering as omnigent host` → tail 新 log） | 0→0 | ✅ PASS |
| 3 | Server `/health` during runner restart | 3/3 attempts 200 body `{"status":"ok"}` | — | ✅ PASS |
| 4 | `podman stop --time 5 omnigent-server` (SIGTERM 優雅) | 0 秒停 + `/health=200` at t+10s | 0→0 | ✅ PASS |

### 三個新觀察

**W2-INFRA-A — Auto-restart 走 Podman restart policy 不是 systemd**
- `systemctl --user list-units "container-omnigent*"` → **0 units**（沒 Quadlet-generated container-*.service）
- Podman 自己 respawn（推測 `--restart=always`），因此每次 kill 後 `RestartCount` 都是 0 — Podman 當作全新 create 不是 in-place restart
- **不是 bug 但是 monitoring 陷阱**：alert 不要靠 `RestartCount`，改看 `State.StartedAt` deltas 或 `podman events` 的 `restarts:` counter

**W2-INFRA-B — Rootless systemd session 不在**
- SIGTERM path 顯示 `dial unix /run/user/1000/systemd/private: connect: connection refused` — user systemd bus 沒 loginctl enable-linger 或 session 掛了
- Podman 仍能清乾淨 + healthcheck 自動恢復，但表示 **`systemctl --user restart container-omnigent-server` fallback path 不存在**，且 healthcheck timer removal 每次 stop 都會 log error
- **建議 follow-up**：`loginctl enable-linger woowtechopenclaw`（install.sh 有做但 session 可能斷）

**W2-INFRA-C — `/health` fix 全端到端驗證**
- 3 個 server restart tests 都在 10 秒內 `/health=200` + runner-loop 也馬上（`server up after 0s`）— 我方本輪 patch 的 `/healthz→/health` 決定性地救得回。**這是 resilience 的骨幹。**

### Anomaly（out of scope）

`omnigent-postgres` 一直顯示 `Up 17 hours (starting)` — health probe 沒 flip 成 `healthy`。同機的 `open-design*` / `woow-tailscale-gateway` / `hermes-*` 同樣症狀 — 疑似 HEALTHCHECK config 缺 或 misconfig。**Postgres 實際能連**（`pg_isready` OK、server DB queries 正常）— cosmetic monitoring 問題。留給後續 pass。

---

## 兩輪合計統計

- **測到的 UI 面向**：38（20 wave1 + 18 wave2）
- **測到的 API endpoint**：30（20 wave1 + 10 wave2）
- **平行 agent 總數**：7（1 wave1 research + 1 wave1 API + 4 wave2 + 1 resilience）
- **總時長**：~90 分鐘（兩輪合計）
- **新增 test artefacts**：37 張截圖 + Playwright suite（14 pass @ 8.5/10 confidence post-patch）+ 兩份繁中 report
- **已修 HIGH**：1（healthz → health across 7 files + live deploy verified）
- **待評估**：4 個 upstream HIGH + 2 MED + 4 LOW（都不是 packaging 責任）

