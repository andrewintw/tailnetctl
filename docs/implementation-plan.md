# tailnetctl 實作計畫

## 1. 目標

建立一個可手動執行的 standalone Python 程式：

1. 從 `.tailscale.env` 讀取 auth key，將本機加入 tailnet。
2. 查詢目前 tailnet 名稱、本機狀態與所有裝置（含自己）。
3. 以互動選單或 `--peer` 指定裝置：
   - 開啟 SSH 互動登入。
   - 建立 SSH 本地端口轉送，讓指定 port 的網頁可透過 `http://127.0.0.1:<local-port>` 存取。

本專案完全獨立，**不依賴**其他專案 SDK，也不封裝成 agent skill。

## 2. 專案結構

```text
tailnetctl/
├── docs/
│   ├── implementation-plan.md   # 本文件
│   └── web-usage.md             # web 的 A/B 使用說明
├── tests/
│   └── test_tailnetctl.py       # unittest 回歸測試
├── .tailscale.env.example        # auth key 與可選設定範例
├── .gitignore                    # 防止誤提交金鑰
├── README.md                     # 使用說明
└── tailnetctl.py                 # 主程式
```

## 3. CLI 設計

```text
tailnetctl.py join   [--auth-key KEY] [--auth-key-file PATH] [--force] [--reset] [--dry-run]
tailnetctl.py status [--json]
tailnetctl.py list   [--json] [--online-only]
tailnetctl.py ssh    [PEER] [--peer PEER] [--remote-user USER] [--ssh-port N] [--ssh-arg ARG]... [--dry-run]
tailnetctl.py web    [PEER] [--peer PEER] [--port PORT] [--local-port N] [--bind ADDR] [--remote-host HOST] [--remote-user USER] [--dry-run]
```

全域選項：

- `--env-file PATH`：預設為程式所在目錄的 `.tailscale.env`。
- `--sudo`／`--no-sudo`：控制是否用 `sudo` 執行 `tailscale` 或啟動 daemon。
- `--no-start-daemon`：只偵測 daemon，不嘗試啟動。
- `--debug`：顯示實際執行的指令；auth key 一律遮蔽。

行為：

- `join`：底層執行 `tailscale up`。已加入 tailnet 時預設只顯示現況；要重新認證或切換 tailnet 必須加 `--force`。`--dry-run` 仍會先讀取狀態，已 `Running` 且未加 `--force` 時只顯示現況。
- `status`：顯示 tailnet 名稱、MagicDNS suffix、本機 hostname／IP／BackendState／Online，以及 peer 總數與 online 數量；不列出 peer 表格。`NeedsLogin` 時提示先 `join`，其他非 `Running` 狀態提示目前未連線；`Running` 但 self 離線時提示顯示的是快取狀態。
- `list`：列出所有裝置（含自己），自己固定第一列並標示 `(self)`；欄位包含序號、hostname、OS、online、Tailscale IP、owner、tags。未連線或本機離線時顯示警告，資訊可能是快取。
- `ssh`／`web`：`BackendState` 必須是 `Running` 且 self 必須在線，否則提示先恢復連線。未給 `PEER` 時進入互動選單；非 TTY 環境需指定 `PEER` 或 `--peer`。選單順序與 `list` 一致。
- `PEER` 支援 hostname、DNSName、Tailscale IP、node ID、選單序號或自己；`PEER` 與 `--peer` 請擇一，不同值會報錯。

## 4. Auth key 設定

預設 env 檔：`<專案根目錄>/.tailscale.env`

`.tailscale.env.example`：

```dotenv
# 複製此檔為 .tailscale.env，並執行 chmod 600 .tailscale.env

# 本機設定
# 在 https://console.tailscale.com/admin/settings/keys 建立 "Auth keys"
TAILSCALE_AUTH_KEY=tskey-auth-REPLACE_ME
TAILSCALE_AUTH_KEY_FILE=
TAILSCALE_HOSTNAME=
TAILSCALE_ADVERTISE_TAGS=
TAILSCALE_ACCEPT_DNS=

# 控制平面
TAILSCALE_LOGIN_SERVER=

# 遠端 peer 連線設定
TAILSCALE_REMOTE_SSH_USER=
TAILSCALE_REMOTE_WEB_PORT=80
```

建立方式：

1. 前往 <https://console.tailscale.com/admin/settings/keys>，在「Auth keys」區段建立 Auth key。
2. 建立 env 檔並填入 key：

```bash
cp .tailscale.env.example .tailscale.env
chmod 600 .tailscale.env
```

優先序：**CLI 參數 > shell 環境變數 > env 檔 > 預設值**。同一來源內，`TAILSCALE_AUTH_KEY_FILE` 優先於 `TAILSCALE_AUTH_KEY`。

env 檔支援整行註解與行內註解；未加引號的 ` #` 之後視為註解，引號內的 `#` 會保留。雙引號值支援 `\"` 與 `\\`，單引號值則原樣保留。

- `TAILSCALE_LOGIN_SERVER` 有值時傳入 `--login-server`，同時支援官方 Tailscale 與自架 Headscale。
- `TAILSCALE_AUTH_KEY_FILE` 必須是檔案路徑（不是 key 本身）；工具以目前使用者讀取後，複製到 `0700` 私有目錄中的 `0600` 暫存檔，再以 `file:` 交給 Tailscale。同一來源內優先於 `TAILSCALE_AUTH_KEY`。
- inline 或 `TAILSCALE_AUTH_KEY_FILE` 的 key 都會寫入 `0700` 私有目錄中的 `0600` 暫存檔，以 `--auth-key=file:...` 傳入，避免 key 出現在 process list，指令結束後刪除整個目錄。
- `join` 缺少 auth key（env 檔不存在、內容為空或仍是 `REPLACE_ME`）時，顯示建立範例檔的指令並結束；`status`／`list` 不需要 auth key。

## 5. 實作重點

### 5.1 Daemon 偵測與啟動

1. 以 `shutil.which()` 尋找 `tailscale` client；找不到時依平台顯示安裝指引，程式不自動安裝。
   - Linux：`curl -fsSL https://tailscale.com/install.sh | sh`
   - 其他平台：`https://tailscale.com/download`
2. 執行 `tailscale status --json` 判斷 daemon 是否可用。
3. 不可用時：
   - 有 systemd：嘗試 `sudo systemctl start tailscaled`，再輪詢 status，最多約 10 秒。
   - 沒有 systemd：才尋找 `tailscaled`；存在時印出可直接執行的 `sudo tailscaled --state=... --socket=...` 指令，不強制背景啟動；缺少時顯示安裝指引。
4. 容器或外部 socket 環境可能只有 `tailscale` client，不要求本機必須有 `tailscaled`。
5. 使用 `--no-start-daemon` 可停用自動啟動。

### 5.2 Join

- 先檢查 `BackendState`；已 `Running` 且未加 `--force` 時直接回報目前 tailnet，不重新認證。`Running` 但 self 離線時提示目前是快取狀態，並提示可用 `--force` 重新認證／加入。
- 指令組合：

  ```text
  tailscale up --auth-key=file:<keyfile>
               [--login-server URL]
               [--hostname NAME]
               [--advertise-tags TAGS]
               [--accept-dns=true|false]
               [--reset]
               [--force-reauth]
  ```

- 只傳入使用者明確設定的選項，避免 `tailscale up` 因「flags 必須完整」而失敗。
- `--force` 會先顯示重新認證／可能中斷連線的警告，並在指令加入 `--force-reauth`。
- `--dry-run` 顯示含 sudo（若適用）的指令；已 `Running` 且未加 `--force` 時只顯示現況。
- 執行後輪詢 `tailscale status --json`，最多等待約 60 秒，確認 `Running` 且 self 在線後印出本機 IP 與 tailnet 名稱。
- auth key、env 內容與完整指令中的 key 都不輸出。

### 5.3 查詢與選單

解析 `tailscale status --json`：

- `CurrentTailnet.Name`／`MagicDNSSuffix`
- `Self`
- `Peer` map
- `User` map

裝置排序：自己優先，其次 online，其次 hostname。

互動選單範例：

```text
#  HOSTNAME        OS       ONLINE  IP              OWNER
1  my-host (self)  linux    yes     100.64.0.10     owner@example.com
2  phone-1         android  yes     100.64.0.20     owner@example.com
3  other-host      linux    no      100.64.0.30     owner@example.com
```

支援 `q` 離開、序號選擇、無效輸入重新詢問。

### 5.4 SSH 連線

- `BackendState` 不是 `Running` 或 self 離線時，直接提示先恢復連線。
- 目標優先使用 peer 的 `DNSName`（去掉結尾 dot）；若無法解析則退回第一個 IPv4。
- 可由 `--remote-user`／`TAILSCALE_REMOTE_SSH_USER` 指定遠端 peer 的登入使用者；未指定時 TTY 詢問，Enter 使用 ssh 預設，`q` 取消。
- 此使用者是連到遠端 peer 時使用的帳號，不是別人連進本機的帳號。
- 以 `os.execvp("ssh", [...])` 交棒給系統 ssh，沿用 `~/.ssh/config`、agent 與 known_hosts。
- 主機參數前加 `--`，並拒絕以 `-` 開頭的遠端使用者／target，避免參數注入。
- 不自動加 `StrictHostKeyChecking=no`；節點重建後 host key 變更時，由使用者確認並用 `ssh-keygen -R` 清除舊記錄。
- 不整合 `tailscale ssh` wrapper：其參數 append 在 host 之後，`-L`／`-N` 端口轉送不可靠；需要時請使用者手動執行 `tailscale ssh user@host`。

### 5.5 Web 端口轉送

- `BackendState` 不是 `Running` 或 self 離線時，直接提示先恢復連線。

執行：

```text
ssh -N -o ExitOnForwardFailure=yes \
    -L <bind>:<local-port>:<remote-host>:<remote-port> \
    -- [user@]<peer>
```

- 預設 `bind=127.0.0.1`、`remote-host=127.0.0.1`、`local-port=8080`。
- 顯式空白的 `--bind`／`--remote-host` 會報錯；非空值會先 trim。
- `--port` 未指定時使用 `TAILSCALE_REMOTE_WEB_PORT`，再退回 `80`。
- `--remote-user`／`TAILSCALE_REMOTE_SSH_USER` 的詢問行為與 SSH 相同。
- 執行前依 `--bind` 印出本機 URL，IPv6 會加方括號：

  ```text
  http://127.0.0.1:8080/
  按 Ctrl-C 結束轉送
  ```

- `--bind 0.0.0.0` 或 `::` 顯示 loopback URL，並在輸出中提醒這會暴露該 port。
- SSH `-L` 的 IPv6 bind／remote-host 會加上方括號，例如 `[::1]:8080:[2001:db8::1]:80`。

## 6. 安全性

- 不列印 auth key；`--dry-run` 顯示 `<redacted>`。
- inline 或 `TAILSCALE_AUTH_KEY_FILE` 的 key 使用 `0700` 私有目錄中的 `0600` 暫存檔，結束即刪除。
- 檢查 `.tailscale.env` 權限；若群組或其他使用者可讀，警告並建議 `chmod 600`。
- 不使用 `shell=True`；所有指令以參數陣列傳遞。
- 不把 key 寫入 log、暫存輸出或版本控制。
- 實際 `join` 會修改本機 tailnet 狀態；由使用者主動執行 `join` 即視為同意，程式不會自動執行。

## 7. 驗證步驟

```bash
python3 -m py_compile tailnetctl.py
python3 -m unittest discover -s tests -v
./tailnetctl.py --help
./tailnetctl.py status
./tailnetctl.py status --json
./tailnetctl.py list
./tailnetctl.py list --json
./tailnetctl.py list --online-only
./tailnetctl.py ssh --peer <peer> --dry-run
./tailnetctl.py web --peer <peer> --port 80 --dry-run
```

另外測試：

- 缺少 auth key 的錯誤訊息與 placeholder key 被拒絕。
- `join --dry-run` 的指令組合、sudo 顯示與 key 遮蔽。
- `join --dry-run --force` 的警告與 `--force-reauth`。
- `ssh`／`web` 在 TTY 詢問遠端使用者；Enter 使用 ssh 預設、`q` 取消。
- 非 TTY 不詢問使用者。
- 互動選單（以 TTY 執行）。
- `NeedsLogin` 與 `Running` 但 self 離線時的提示與阻擋。
- `PEER` 與 `--peer` 衝突、IPv6 URL／`-L` 格式、POSIX 平台檢查。
- env 檔行內註解、引號內 `#` 保留、`value#fragment` 保留。

實際 `join` 需使用者提供有效 auth key 並主動執行，才進行本機寫入。

## 8. 假設與風險

- 需要 Python 3.8+（使用 `typing.TypedDict` 與 `Literal`）。
- 假設主機為 POSIX 系統（Linux／macOS）；Windows 請使用 WSL。daemon 自動啟動以 systemd 為主，其他 POSIX 平台需先自行啟動 Tailscale。
- 假設主機已安裝 `tailscale` client；若需要本機啟動 daemon，另需 `tailscaled` 或 systemd service。未安裝時只提示，不自動安裝。
- 假設主機可連到控制平面且時間正確；否則 join 會失敗，程式會顯示 Tailscale 的錯誤。
- `tailscale status --json` 欄位可能隨版本變動，解析採防禦性寫法。
- 已加入 tailnet 的主機，`join` 不會在未加 `--force` 時切換 tailnet。
- 節點重建可能改變 SSH host key；需先確認變更，再用 `ssh-keygen -R` 清除舊記錄，不應關閉 host key 檢查。
- SSH 本地轉送的 `127.0.0.1` 只綁在本機；要從另一台機器看網頁，需 `--bind 0.0.0.0`／`::` 或再建一層 SSH tunnel。
