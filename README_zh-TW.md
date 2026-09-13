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

## 收斂手動編輯過的安裝

woowtechopenclaw 上的 omnigent 已經是 Quadlet——只是那些單元是手寫的，而且後來又被就地改過。
容器名稱、磁碟區（`omnigent-postgres-data`、`omnigent-server-data`）與網路（`omnigent`）都已經
是本 repo 宣告的那一組，所以**沒有東西需要遷移**：沒有舊容器要改名或 capture，也沒有資料需要用
別的名字沿用。因此本 repo 不提供 `migrate-legacy.sh`。那台主機需要的是**收斂**，而收斂就是
`scripts/install.sh`：`ql_install_files` 會在寫入我們的檔案前，先把同名的外來檔案備份一份；
`ql_apply_units` 只重啟檔案真的變了的單元。這正是 `Woow_podman_pi_agent_package` 在 toypark1234
上走過的路——把手改過的 `pi-web.container` 重新指向 `%h`/`%t`，代價是 1.5 秒。

```bash
scripts/converge.sh --check      # 前置檢查 + 偏移報告 + install.sh --dry-run
scripts/converge.sh              # 備份、收編 secrets、install.sh、驗證、回報
scripts/converge.sh              # 再跑一次：「files changed : none」，零停機
scripts/converge.sh --status
scripts/converge.sh --rollback   # 放回先前的單元檔，用舊映像重新啟動
```

`scripts/converge.sh` 自己不安裝任何東西。它只是在那一次 `install.sh` 外面，補上操作者面對線上
服務時需要的證據：

**前置檢查寧可拒絕也不猜測。** 三個容器都必須存在、在執行中，且帶有
`PODMAN_SYSTEMD_UNIT=<名稱>.service`；其他情況都屬於「遷移」而被拒絕。兩個磁碟區都必須存在。
發布位址、連接埠、base URL、管理員帳號與 pi-state 模式，全部讀自執行中的容器，不用 repo 的預設值。

**逐檔的偏移報告，在任何重啟之前。** 它會列出手寫單元有、而本 repo 沒有的東西。沒有命中任何標記的
檔案會標成 *no named drift*，而不是「相同」：`install.sh` 比對的是位元組，仍可能因為報告叫不出名字的
差異（少了 `Label=`、鍵的順序不同）而重寫它。真正權威的清單是 `--check` 的 `[dry-run] would write`
輸出。在 openclaw 上，報告會列出：
浮動 tag（`postgres:16-alpine`、`omnigent-server:latest`、`woow-omnigent-runner:latest`）、
`AutoUpdate=local`、`Environment=POSTGRES_PASSWORD=…` 與內含密碼的 `DATABASE_URL`、缺少
`SuccessExitStatus=143`，以及把 `pi-agent-data` 寫成裸名稱而非 `.volume` 單元。

**收編 secrets——這一步不能跳過。** Postgres 角色的密碼是 `initdb` 在那個要被沿用的磁碟區上設定的：
只有存著**那個**密碼的 secret 才打得開它。`converge.sh` 從執行中的容器讀出來，直接 pipe 進
`podman secret create`——不經過 argv、journal 或 `set -x`。`install.sh` 在磁碟區存在但 secret 不存在
時會拒絕執行，這是刻意的；`converge.sh` 正是滿足那個拒絕條件的人。`OMNIGENT_ADMIN_PASSWORD` 也以
同樣方式收編，runner 才登得進去。`--check` 也會建立這兩個 secret——它們是從已在執行的東西推導出來的
附加式 podman secret，少了它們 `install.sh --dry-run` 什麼都渲染不出來，一個略過這步的 `--check`
等於什麼都沒驗證。它仍然不會動到任何單元檔、容器或服務。

**先備份，並附校驗碼。** `~/backups/omnigent/converge-<timestamp>/` 內含真正的 `pg_dump -Fc`
（對執行中的 PGDATA 做磁碟區匯出會是破碎的副本）、`pg_dumpall --roles-only`、兩個磁碟區的匯出、
每個即將被覆寫的單元檔副本、`podman inspect` 與 `precheck.txt`——全部列進 `SHA256SUMS`，權限
`0700`/`0600`。

**證明資料確實被沿用。** `.volume` 是**用名稱**沿用；唯有名稱仍指向同一個目錄，這才值得相信。
每個磁碟區的 `CreatedAt` 與掛載點 inode 都在事前記錄、事後比對。不符即判定收斂失敗並自動回復。

**量測停機時間。** 探測器每 100 毫秒從外部取樣 `/health`；重啟前最後一次成功到之後第一次成功之間
的間隔，就是回報的停機時間。

### `pi-agent-data` 不是我們的

在 `OMNIGENT_PI_STATE=shared` 之下，runner 掛載的是
[Woow_podman_pi_agent_package](https://github.com/WOOWTECH/Woow_podman_pi_agent_package) 的
`pi-agent-data`，而 `pi-web`（兩台主機上的線上服務）也掛載同一個磁碟區。本套件只**引用**那個磁碟區
單元，從不擁有它：不提供 `pi-agent-data.volume`、`install.sh` 不會把它納入安裝清單，這裡也沒有任何
程式會啟動、重啟、停止或移除 `pi-agent-data-volume.service` 或 `pi-web.service`。`converge.sh` 會在
事前事後記錄 `pi-web` 的容器 id 與 `StartedAt`，以及磁碟區的識別碼，任一改變就判定失敗——所以
「pi-web 沒被動到」是日誌裡的量測值，不是假設。`tests/converge-model.local.sh` 把這些全部釘住。

### 回復

`scripts/converge.sh --rollback` 會把儲存的單元檔放回去，reload 並重啟三個服務。收斂過程不會移除
任何映像，所以舊的浮動 tag 仍在主機上，還原後的單元啟動的就是它先前跑的東西。除非事後輪替過
secrets，否則資料庫完全未受影響；若已輪替，請一併從備份目錄還原 `omnigent.pgdump`。

### 重跑是安全的，而且不會移動回復點

什麼都沒改變的收斂仍然會做一次新的備份（不論如何，一份 `pg_dump` 與磁碟區匯出都值得留著），但只有
真正替換過單元檔的那一次才會成為 `--rollback` 的目標。否則第二次、也就是文件要你做的那次 no-op
執行，會把儲存的收斂前單元悄悄換成已收斂的版本，毀掉唯一的退路。

### 之後

被收編的密碼原本就寫在舊單元檔裡——以明文存在磁碟上，也存在每一份備份裡。請用
`scripts/rotate-secrets.sh --all` 輪替，然後執行 `tests/smoke.sh`。

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
scripts/converge.sh     手動安裝的 Quadlet 主機 → 本 repo 的單元：前置檢查、備份、收編 secret、
                        沿用證明、量測停機、--rollback
scripts/converge-lib.sh 它與 tests/ 共用的偏移、備份、還原與停機量測輔助函式
tests/                  dryrun.sh（+ dryrun.local.sh、fixtures/）、smoke.sh、
                        smoke-{container,pi-integration,runner-dialin}.sh、e2e/（Playwright）
tests/converge-model.sh 以 shim 驅動：偏移偵測、備份往返、停機量測，以及定義「收斂完成」的性質
                        ——第二次執行什麼都不會變
tests/shims/            podman 與 systemctl 測試替身（不會建立任何容器）
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
