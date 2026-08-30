# Woow Podman Omnigent

[![Podman](https://img.shields.io/badge/Podman-%E2%89%A54.4%20rootless-892CA0)](https://podman.io)
[![Quadlet](https://img.shields.io/badge/units-Quadlet%20%2B%20systemd-orange)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
[![Omnigent](https://img.shields.io/badge/omnigent-0.11.0-blueviolet)](https://github.com/omnigent-ai/omnigent)
[![pi-coding-agent](https://img.shields.io/badge/pi--coding--agent-0.83.0-blue)](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) · **繁體中文**

把 [Omnigent](https://github.com/omnigent-ai/omnigent)（open-source meta-harness）
封裝成 rootless Podman 三容器組：**Postgres**、上游 **server**、跟一個
**always-on runner**。Runner 掛共用 `pi-agent-data` volume、把 `pi` harness
交出去給 Omnigent 用。跟 [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package)、
[`Woow_podman_opendesign`](https://github.com/WOOWTECH/Woow_podman_opendesign)、
[`Woow_podman_code_server_package`](https://github.com/WOOWTECH/Woow_podman_code_server_package)
共用同一份 pi state——一份 state、四個消費者。

---

## 提供什麼

| | |
|---|---|
| **Web UI** | `http://127.0.0.1:8000/`（loopback）；tailnet 真 HTTPS via `tailscale serve --https=9444` |
| **Server** | 上游 `ghcr.io/omnigent-ai/omnigent-server:latest` — 不自 build，跟上游版本走 |
| **Database** | 自己一顆 `postgres:16-alpine`，自己一顆 volume，不對外 publish |
| **Runner** | 常駐 sidecar，內含 pi 0.83.0 + `pi-code` wrapper，`OMNIGENT_PI_PATH` 指到共用 `/data/pi-agent` volume |
| **Auth** | 上游 built-in accounts 流程（第一次開機建 admin 帳密），見 [First boot](#first-boot) |
| **監管** | `systemd --user` Quadlet + 30 秒 server 健康檢查 timer |

---

## 為什麼要 sidecar runner

Omnigent 架構跟 code-server / OD 不一樣。Server 只做編排跟 Web UI，
**agent 執行在另一個 runner 進程**、透過 WebSocket dial in。兩條路：

1. 用戶自己筆電跑 `omnigent run --harness pi`——每個人各自的 pi state
2. **本 addon**：`.197` 上跑一個常駐 runner、掛共用 volume。Web UI 開的
   任何 session 選 pi 就能用、provider / skills 都是 pi-web 早設好的

我們選 (2)——這樣 Omnigent 的 pi 體驗跟 pi-web / OD / code-server 完全一致，
四邊都透過 `pi-code` HOME-rescoping wrapper 共用同一份磁碟。

完整決策紀錄見 [`docs/plans/2026-08-30-initial-package.md`](docs/plans/2026-08-30-initial-package.md)。

---

## 安裝

需要 Podman ≥ 4.4 (Quadlet)、rootless。**強烈建議**先裝 [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package)
——那個 addon 建立 `pi-agent-data` volume、把 provider / skills 準備好；沒裝的話
本 repo 會建一顆空 volume，你要從頭設 provider。

```bash
git clone https://github.com/WOOWTECH/Woow_podman_omnigent_package.git
cd Woow_podman_omnigent_package
./scripts/install.sh
```

啟動順序：`omnigent-postgres` → `omnigent-server` → `omnigent-runner`。
install.sh 會等 server 回應 `/health`（JSON `{"status":"ok"}`）才收工，
接著**自動 claim 第一個 admin**：讀 `quadlet/omnigent-runner.container`
裡的 `OMNIGENT_ADMIN_{USERNAME,PASSWORD}` POST 給 `/auth/setup`，不用手動
開 `/setup` 表單，runner container 起來後 curl `/auth/login` 就打得中。
若 admin 已存在會收 409 靜默略過。

**不要**把 `/healthz` 當 liveness probe — React SPA catch-all 把 `/healthz`
變成回 `index.html` HTTP 200，即使 API 死了 probe 也永遠綠燈。

**部署到信任邊界以外前先改預設密碼** — 預設值放在
`quadlet/omnigent-runner.container`，同時是 runner 撥回登入用的憑證跟
人類 admin 帳號的 single source of truth。

跳過 runner image 重 build：`OD_SKIP_BUILD=1 ./scripts/install.sh`。

## First boot

上游 server 不會自動生 admin 密碼。第一次開機看 `podman logs omnigent-server`
會有一行 `"No admin yet"` 指到 base URL——開那個 URL、填 Create-admin 表單
（username + password）。

想避開「首個訪客搶佔」的 race window，可以在跑 install.sh 前，先取消
`quadlet/omnigent-server.container` 內的 `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`
註解、設個密碼。

## Tailnet HTTPS

Omnigent 的 Web UI 是純 React SPA（不吃 ServiceWorker webview），plain HTTP
其實就行——但為了跟 code-server 的 HTTPS 故事一致：

```bash
podman exec woow-tailscale-gateway \
    tailscale serve --bg --https=9444 http://127.0.0.1:8000
```

然後開 **`https://woow-openclaw-services-1.tailb7a69b.ts.net:9444/`**
——Tailscale API 幫你申請的真 Let's Encrypt 憑證，瀏覽器自動信任、
無自簽警告。

## 移除

```bash
./scripts/uninstall.sh          # 停 + 移；保留 omnigent-{server,postgres}-data
./scripts/uninstall.sh --purge  # 連 data volume 也刪
```

`pi-agent-data` **不會被動**（那是姊妹 pi-web 的）。

---

## 目錄結構

```
Containerfile.runner         Debian 12 slim + Node 22 + pi 0.83.0 + omnigent 0.11.0 + pi-code
quadlet/
  omnigent.network           3 個 container 共用的私有 podman bridge
  omnigent-postgres.container    postgres:16-alpine、不 publish、pg_isready 健康檢查
  omnigent-server.container      上游 ghcr image、publish 127.0.0.1:8000
  omnigent-runner.container      本 repo 自 build image、dial in server、UserNS keep-id
rootfs/
  usr/local/bin/pi-code              HOME wrapper（跟 code-server 一樣）
  usr/local/bin/omnigent-runner-loop long-lived runner entrypoint（現場迭代中）
systemd/
  omnigent-server-health.{service,timer}   30 秒 podman healthcheck 更新
scripts/
  install.sh                 build runner + install units + 依序啟動
  uninstall.sh               down；--purge 刪 data volume
tests/
  smoke-container.sh         3 個 up + server /health + pg_isready
  smoke-pi-integration.sh    pi + wrapper + volume + env 都對
  smoke-runner-dialin.sh     runner 可從私有網段連到 server
docs/plans/                  塑造本 package 的設計決策
.github/workflows/build.yml  amd64 + arm64 CI，push/release 到 ghcr
```

---

## 驗收部署

```bash
bash tests/smoke-container.sh          # 3 up、/health 200 JSON、pg_isready
bash tests/smoke-pi-integration.sh     # pi + wrapper + volume + env
bash tests/smoke-runner-dialin.sh      # runner → server 通
```

三支全綠是健康部署。姊妹 artefact 那幾條若 volume 空會 skip 不 fail。

---

## 日常操作

```bash
podman ps --format '{{.Names}}\t{{.Status}}'
journalctl --user -u omnigent-server -f
podman logs -f omnigent-server         # Web UI + 編排事件
podman logs -f omnigent-postgres       # db
podman logs -f omnigent-runner         # agent runner 狀態
podman exec -it omnigent-runner bash   # 進 runner shell（找真正 runner CLI）
systemctl --user restart omnigent-server  # 只重啟 server
```

升版：
- **Server**：改 `quadlet/omnigent-server.container` 的 `Image=`（可 pin
  `sha-<short>` 或 `vX.Y.Z`）→ `systemctl --user restart omnigent-server`
- **Runner (pi / omnigent)**：改 `Containerfile.runner` 的 ARG、`./scripts/install.sh`
  （自動 rebuild）、`systemctl --user restart omnigent-runner`

pi CLI 版本必須跟姊妹 `Woow_podman_pi_agent_package` 同步——共用 volume
的 on-disk schema 沒版本化。

---

## 安全性

- **Server loopback publish**——對外門戶是 Tailscale HTTPS，不直接對 LAN 開
- **Postgres 不 publish**——只有 omnigent podman network 內互通
- **Runner UserNS keep-id** 讓 host uid 1000 跟 container uid 1000 對齊，
  共用 volume 讀寫沒 chown 麻煩。同 code-server pattern。
- **`OMNIGENT_AUTH_ENABLED=1`**——多用戶 built-in accounts。**不要**設成 `0`
  除非部署完全在信任 LAN 內
- **首開機 race window**：admin 名單空的時候，`POST /auth/setup` 是無驗證的。
  要嘛預先 seed `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD`，要嘛開了 URL 立刻自己搶

---

## 相關套件

- [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package) — 建 `pi-agent-data` volume 的 pi-web 姊妹
- [`Woow_podman_opendesign`](https://github.com/WOOWTECH/Woow_podman_opendesign) — OpenDesign 版，同 volume 共用 pattern
- [`Woow_podman_code_server_package`](https://github.com/WOOWTECH/Woow_podman_code_server_package) — code-server + ACP Client extension，同 pi-code wrapper
- [Omnigent（上游）](https://github.com/omnigent-ai/omnigent) — Apache 2.0 meta-harness

## 授權

MIT
