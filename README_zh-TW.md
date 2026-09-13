# Woow Podman Omnigent

[![Podman](https://img.shields.io/badge/Podman-%E2%89%A54.9%20rootless-892CA0)](https://podman.io)
[![Quadlet](https://img.shields.io/badge/units-Quadlet%20%2B%20systemd-orange)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
[![Omnigent](https://img.shields.io/badge/omnigent-0.12.0-blueviolet)](https://github.com/omnigent-ai/omnigent)
[![pi-coding-agent](https://img.shields.io/badge/pi--coding--agent-0.85.1-blue)](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) · **繁體中文**

把 [Omnigent](https://github.com/omnigent-ai/omnigent)（open-source meta-harness）封裝成
rootless Podman 三容器組，由 systemd 監管：**Postgres**、上游 **server**，以及一個
**常駐 runner**，讓 Web UI 開的每個 session 都能選 `pi` harness。

> ### 請輪替曾被提交的憑證
>
> 在這次變更之前，`quadlet/omnigent-postgres.container`、`-server.container`、
> `-runner.container` 以明碼帶著資料庫密碼與 admin 帳密，而且 `install.sh` 會在首次開機用它
> 建立 admin。**那些值存在一個公開 repository 的 git 歷史中，無法刪除。** 任何跑過它們的主機
> 都必須立刻輪替：
>
> ```bash
> scripts/rotate-secrets.sh --all      # 產生新的資料庫密碼；在 Web UI（Settings -> Account）
>                                      # 改完 admin 密碼後再輸入新密碼
> ```
>
> 其他使用相同字串的系統也應視為已外洩。新安裝會自行產生 podman secret，不會把明碼寫到磁碟。

---

## 提供什麼

| | |
|---|---|
| **Web UI** | 預設 `http://127.0.0.1:8000/`；對外門面是同一台主機上的 tailnet 或 tunnel |
| **Server** | 上游 `ghcr.io/omnigent-ai/omnigent-server:v0.12.0`，版本固定，不自行建置 |
| **Database** | 自己的 `postgres:16.15-alpine3.24` 容器與 volume，永不對外 publish |
| **Runner** | 常駐 sidecar，內含 pi 0.85.1 與 `pi-code` wrapper，掛在 `/data/pi-agent` |
| **Secrets** | 安裝時產生的 podman secret：資料庫密碼（檔案掛載）、`DATABASE_URL`、admin 密碼 |
| **監管** | `systemd --user` Quadlet 單元、Postgres 真正的就緒 gate、30 秒健康檢查 timer |

---

## 安裝

rootless podman >= 4.9（在 4.9.3 測試）、systemd 255、啟用 linger，另需 `curl` 與 `jq`。

```bash
git clone https://github.com/WOOWTECH/Woow_podman_omnigent_package.git
cd Woow_podman_omnigent_package
scripts/install.sh                       # 或：scripts/install.sh --port 18000
```

`scripts/install.sh` 可重複執行，它會：

1. 檢查主機（不是 root、podman >= 4.9、有 Quadlet 產生器、`systemctl --user` 可用）並啟用 linger；
2. 第一次執行時從 [`config/omnigent.env.example`](config/omnigent.env.example) 建立
   `~/.config/omnigent/omnigent.env`（0600）；`--port N`、`--bind ADDR`、`--pi-state MODE`、
   `--set KEY=VALUE` 會修改設定並存回該檔；
3. 若已有不受 Quadlet 管理、名為 `omnigent-postgres`／`omnigent-server`／`omnigent-runner`
   的容器（Quadlet 用 `podman run --replace` 啟動），或選用的埠號已被占用，或
   `omnigent-postgres-data` volume 存在但沒有對應的密碼 secret，就拒絕繼續；
4. 用 env 檔渲染 [`quadlet/`](quadlet/) 與 [`systemd/`](systemd/)（`@@VAR@@` 標記，白名單在
   `quadlet/render-vars`），並在安裝任何東西 **之前** 用 podman 4.9.3 產生器與
   `systemd-analyze --user verify` 檢查；
5. 當 `localhost/woow-omnigent-runner:<VERSION>` 不存在時建置（`--rebuild` 強制、`--no-build`
   禁止），並先拉好固定版本的 server 與 Postgres 映像；
6. 缺少時建立 podman secret，並且每次都由資料庫密碼重新推導 `DATABASE_URL`，兩者不會不同步；
7. 只安裝有變更的檔案，只重啟檔案有變更的單元；
8. 等 `/health`，用產生的密碼透過 `POST /auth/setup` **建立第一個 admin**（密碼由 secret 以
   pipe 傳入，不會出現在命令列），等兩個容器 healthy，然後執行
   [`tests/smoke.sh`](tests/smoke.sh)。

`scripts/install.sh --dry-run` 會渲染並驗證、回報將變更什麼，但不做任何變更。

### 首次登入

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' omnigent-admin-password
```

透過 tailnet／tunnel 開啟 Web UI（或 `ssh -L 8000:127.0.0.1:8000 <host>` 後開
<http://localhost:8000>），以 env 檔中的 `OMNIGENT_ADMIN_USERNAME` 登入。private pi 模式下，
替 runner 做一次 pi 登入：

```bash
podman exec -it omnigent-runner pi login
```

### 設定

編輯 `~/.config/omnigent/omnigent.env` 後重新執行 `scripts/install.sh`。

| 鍵 | 預設 | 說明 |
|----|------|------|
| `OMNIGENT_BIND` | `127.0.0.1` | 發布位址；可用區網 IP，拒絕 `0.0.0.0`。 |
| `OMNIGENT_PORT` | `8000` | Web UI 的主機埠號。 |
| `OMNIGENT_ACCOUNTS_BASE_URL` | *（空）* | 邀請連結與 OAuth 轉址用的公開／tailnet URL；留空由上游自行推導。 |
| `OMNIGENT_ADMIN_USERNAME` | `admin` | install.sh 建立、runner 用來登入的帳號。 |
| `OMNIGENT_PI_STATE` | `private` | `private`（自己的 volume）或 `shared`（pi-web 的 `pi-agent-data`）。 |

### pi state：private 或 shared

- **private**（預設）：runner 使用本套件擁有的 `omnigent-pi-data`。需要在 runner 內做一次
  `pi login`，會被 `scripts/backup.sh` 備份，`uninstall.sh --purge` 會刪除它。不與 pi-web 共用，
  因此沒有 pi 版本落差問題。
- **shared**（`--pi-state shared`）：runner 掛載
  [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package)
  擁有的 **`pi-agent-data`**，看得到 pi-web 的 provider 登入、sessions 與 skills。此時 runner 單元
  *參照* 那個套件的 `pi-agent-data.volume`，因而取得真正的
  `Requires=pi-agent-data-volume.service`；若該單元未安裝，install.sh 會拒絕此模式（在 podman
  4.9.3 上，指向不存在的 `.volume` 會無聲變成空的 `systemd-pi-agent-data`）。本套件不會安裝、
  標記、備份或刪除 `pi-agent-data`：**`--purge` 碰不到它。** 請讓兩邊的 pi 版本保持一致，
  磁碟格式沒有版本控制。

### 啟動順序

先是 `omnigent-network` 與各 volume 單元，接著 `omnigent-postgres`——它要等到
`ExecStartPost=` 的 gate 看到 Postgres 接受 TCP 連線才會變成 *active*（podman 4.9.3 會忽略
`Notify=healthy`，這個 gate 才讓 `Requires=`／`After=` 真的有意義），然後是 `omnigent-server`，
最後是 `omnigent-runner`。

---

## Secrets

| podman secret | 內容 | 容器如何取得 |
|---|---|---|
| `omnigent-postgres-password` | 資料庫角色密碼 | `type=mount` + `POSTGRES_PASSWORD_FILE`，不會出現在 `podman inspect` |
| `omnigent-database-url` | `postgresql+psycopg://omnigent:<密碼>@omnigent-postgres:5432/omnigent` | server 的 `type=env DATABASE_URL` |
| `omnigent-admin-password` | admin 帳號密碼 | runner 的 `type=env OMNIGENT_ADMIN_PASSWORD` |

三者都在安裝時由 `/dev/urandom` 產生，透過 pipe 建立（不經過 argv、log 或 xtrace），
`install.sh` 不會覆寫既有的值。env 檔裡沒有任何密碼。

**注意（podman 4.9.3，已驗證）**：`type=env` 的 secret 值 *會* 出現在執行中容器的
`podman inspect`，所以只要能使用這個使用者的 podman socket（例如本機的 podman MCP server），
就能讀到 `DATABASE_URL` 與 admin 密碼。它們仍然不在 git、單元檔、`systemctl --user cat`、
容器建立指令與 journal 中。資料庫密碼本身以檔案掛載，不受此影響。

輪替：

```bash
scripts/rotate-secrets.sh --db       # ALTER ROLE + 兩個資料庫 secret + 重啟 server
scripts/rotate-secrets.sh --admin    # 在 Web UI 改完密碼之後
scripts/rotate-secrets.sh --all
```

---

## Tailnet HTTPS

Web UI 是單純的 React SPA，但瀏覽器信任的來源仍是正確的門面。在這台主機所用的 tailscale 節點
（WOOWTECH 這邊是 `woow-tailscale` 容器）上：

```bash
podman exec woow-tailscale tailscale serve --bg --https=9444 http://127.0.0.1:8000
```

然後在 env 檔設定 `OMNIGENT_ACCOUNTS_BASE_URL=https://<node>.<tailnet>.ts.net:9444` 並重新執行
`scripts/install.sh`，讓邀請連結與轉址指向使用者實際用的名稱。

---

## 升級

```bash
git pull
scripts/upgrade.sh
```

它會先為已安裝單元做快照、執行 `scripts/backup.sh`（pg_dump 加 volume 匯出）、執行
`scripts/install.sh` 與 `tests/smoke.sh`；任何一步失敗就放回先前的單元、以先前的映像 tag 重啟。
上游的資料庫 migration 是單向的：若新版 server 已經改了 schema，回復時也需要那份 dump，
腳本會印出對應的 `scripts/restore.sh` 指令。

升級 server 時要同時改 `quadlet/omnigent-server.container` 的 `Image=`、`Containerfile.runner`
的 `ARG OMNIGENT_VERSION` 與 `VERSION`；三者不一致時 `tests/dryrun.sh` 會失敗。

## 備份與還原

```bash
scripts/backup.sh                     # -> ~/backups/omnigent/<時間戳>/
scripts/backup.sh --include-secrets   # 另外存資料庫與 admin 密碼（secrets.env，0600）
scripts/backup.sh --stop              # 匯出 volume 前先停 runner 與 server，匯出完再啟動
scripts/restore.sh ~/backups/omnigent/<時間戳> [--with-secrets]
```

備份內容是資料庫的 `pg_dump -Fc`、`omnigent-server-data` 匯出、private 模式下的
`omnigent-pi-data` 匯出，以及 env 檔副本。`restore.sh` 會停止 runner 與 server、依 dump 重建
資料庫、取代 volume，並執行 smoke 測試。shared 模式的 `pi-agent-data` 屬於 pi-web 套件，
這裡不備份也不還原。

## 解除安裝

```bash
scripts/uninstall.sh                   # 停止並移除單元；保留 volume、secret、映像、env 檔
scripts/uninstall.sh --purge           # 另外刪除 omnigent 的 volume、network 與 secret
scripts/uninstall.sh --purge-images    # 另外移除 localhost/woow-omnigent-runner:* 映像
```

`--purge` 是這些腳本刪除資料的唯一方式。它會先做一次完整備份，並要求輸入應用名稱確認
（`--yes` 可略過）。它永遠不會碰 `pi-agent-data`。

## 遷移既有部署

適用於已在跑舊版單元的主機（woowtechopenclaw）：

1. **先輪替**（見文件開頭的方塊），至少也要在遷移後立刻做。
2. 備份：`podman exec omnigent-postgres pg_dump -U omnigent -d omnigent -Fc > ~/omnigent-pre-quadlet.dump`
   （0600）、匯出 `omnigent-server-data`，並保留一份舊單元檔。
3. 用目前部署實際使用的密碼建立資料庫 secret，讓沿用的 volume 仍可開啟——直接從容器讀出並以
   pipe 傳入，不要印出來：

   ```bash
   podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' omnigent-postgres \
     | sed -n 's/^POSTGRES_PASSWORD=//p' | tr -d '\n' \
     | podman secret create --label io.woowtech.app=omnigent omnigent-postgres-password -
   ```

   `omnigent-admin-password` 同理，從 `omnigent-runner` 的 `OMNIGENT_ADMIN_PASSWORD` 取得。
   `omnigent-database-url` 由 `install.sh` 自行推導。
4. 把舊的 plain 單元移開（它們不在本套件的 manifest 中，install.sh 會拒絕覆寫）：
   `systemctl --user disable --now omnigent-server-health.timer`，再
   `mv ~/.config/systemd/user/omnigent-server-health.{service,timer} ~/`。
5. 寫入該主機的設定並安裝：

   ```bash
   scripts/install.sh --pi-state shared --set OMNIGENT_ADMIN_USERNAME=<目前的 admin> \
     --set OMNIGENT_ACCOUNTS_BASE_URL=<目前的 base URL>
   ```

   容器、volume 與 network 名稱都沒變，資料會被沿用。server 固定的 v0.12.0 就是該主機目前執行的
   digest，因此沒有版本跳躍；runner 映像以相同 ARG 重新建置成固定 tag。
6. 執行 `tests/smoke.sh`，然後 `scripts/rotate-secrets.sh --all`。

回復：放回保存的單元檔（舊映像 tag 仍在）、`daemon-reload`、重啟。除非做過輪替，資料庫不受影響。

---

## 目錄結構

```
Containerfile.runner    python 3.12.14 + Node 22 + pi + omnigent + pi-code（base 版本固定）
VERSION                 <omnigent 版本>-<套件修訂>，即 runner 映像 tag
quadlet/                omnigent.network、omnigent-{postgres,server,pi}.volume、
                        omnigent-{postgres,server,runner}.container、render-vars
systemd/                omnigent-server-health.{service,timer}：30 秒健康狀態更新
config/                 omnigent.env.example
rootfs/usr/local/bin/   pi-code（HOME 重導 wrapper）、omnigent-runner-loop
scripts/                install、upgrade、uninstall、backup、restore、rotate-secrets、
                        render-args.sh；lib/quadlet-lib.sh（vendored，校驗和固定）
tests/                  dryrun.sh（+ dryrun.local.sh、fixtures/）、smoke.sh、
                        smoke-{container,pi-integration,runner-dialin}.sh、e2e/（Playwright）
```

## 驗證部署

```bash
tests/dryrun.sh                    # 渲染並檢查單元；不啟動容器
tests/smoke.sh                     # 單元、健康、只聽 loopback、admin 登入、runner 註冊
bash tests/smoke-container.sh      # 三個容器都在、/health、pg_isready
bash tests/smoke-pi-integration.sh # pi 版本、pi-code wrapper、/data/pi-agent、OMNIGENT_PI_PATH
bash tests/smoke-runner-dialin.sh  # runner 能透過私有網路連到 server
```

`tests/e2e/` 是 Web UI 的 Playwright 測試，需要環境變數 `OMNIGENT_BASE_URL` 與
`OMNIGENT_ADMIN_PASSWORD`，本身不含任何憑證。

## 日常操作

```bash
podman ps --format '{{.Names}}\t{{.Status}}'
journalctl --user -u omnigent-server -f
podman logs -f omnigent-runner                 # 登入、host 註冊、harness 活動
systemctl --user restart omnigent-server       # runner 會透過 Requires= 一起重啟
```

## 安全性

- 預設 **只發布在 loopback**；對外門面是本機的 tailnet 或 tunnel。
- **Postgres 永不對外 publish**，只在私有 omnigent 網路上可達。
- repo、單元檔與 journal 中 **沒有明碼憑證**（見 Secrets）。
- **`OMNIGENT_AUTH_ENABLED=1`**：內建帳號流程。任何超出本機可達的部署都不要設成 `0`。
- **首次開機的空窗**：admin 名冊為空時 `POST /auth/setup` 不需驗證。install.sh 會在 server 回應
  `/health` 後數秒內建立 admin，而且當下 server 只在 loopback 上。
- runner 使用 **UserNS keep-id**，讓 volume 上 pi 的檔案維持主機使用者所有。

## 相關套件

- [`Woow_podman_pi_agent_package`](https://github.com/WOOWTECH/Woow_podman_pi_agent_package) — pi-web；shared 模式所用 `pi-agent-data` volume 的擁有者
- [`Woow_podman_code_server_package`](https://github.com/WOOWTECH/Woow_podman_code_server_package) — code-server，使用相同的 `pi-code` wrapper
- [`Woow_podman_opendesign`](https://github.com/WOOWTECH/Woow_podman_opendesign) — OpenDesign
- [Omnigent（上游）](https://github.com/omnigent-ai/omnigent) — Apache 2.0 meta-harness

## 授權

MIT
