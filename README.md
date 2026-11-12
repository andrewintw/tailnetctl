# tailnetctl

`tailnetctl.py` 是一個 standalone Python 工具，用來：

- 從 `.tailscale.env` 讀取 auth key，將本機加入 tailnet。
- 查詢 tailnet、本機狀態與所有裝置（含自己）。
- 透過互動選單或 `--peer` 選擇裝置，開啟 SSH 或建立本地端口轉送。
- 支援官方 Tailscale 與自架 Headscale（透過 `TAILSCALE_LOGIN_SERVER`）。

本專案不依賴其他 SDK。

## 需求

- Linux 或 macOS（POSIX）；Windows 請使用 WSL
- Python 3.8+
- `tailscale` client
- 本機 daemon（通常是 `tailscaled` 或 systemd service；若已有外部 daemon 則不需要）
- OpenSSH client（`ssh`）

daemon 自動啟動以 systemd 為主；macOS 或其他 POSIX 平台請先自行啟動 Tailscale。

程式不會自動安裝 Tailscale。找不到 `tailscale` 時會依平台顯示安裝指引：

Linux：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

其他平台：<https://tailscale.com/download>

## 快速開始

```bash
cp .tailscale.env.example .tailscale.env
chmod 600 .tailscale.env
```

請先至 [Auth keys](https://console.tailscale.com/admin/settings/keys) 建立 Auth key，再編輯 `.tailscale.env` 填入 `TAILSCALE_AUTH_KEY`：

```dotenv
TAILSCALE_AUTH_KEY=tskey-auth-REPLACE_ME
```

接著執行：

```bash
./tailnetctl.py join
./tailnetctl.py status
./tailnetctl.py list
```

未給子指令時會顯示完整 help 與範例：

```bash
./tailnetctl.py
```

## 環境變數欄位

預設 env 檔為程式所在目錄的 `.tailscale.env`，可用 `--env-file PATH` 指定其他位置。

優先序：**CLI 參數 > shell 環境變數 > env 檔 > 預設值**。

env 檔支援整行註解與行內註解；未加引號的 ` #` 之後視為註解，引號內的 `#` 會保留。雙引號值支援 `\"` 與 `\\`，單引號值則原樣保留。

| 欄位 | 範圍 | 必填 | 用途 |
| --- | --- | --- | --- |
| `TAILSCALE_AUTH_KEY` | 本機 | `join` 時需要 | Tailscale auth key。可至 [Auth keys](https://console.tailscale.com/admin/settings/keys) 建立。實際 `join` 時會建立 `0700` 私有目錄中的 `0600` 暫存檔，以 `--auth-key=file:...` 傳入，指令結束後刪除；不會列印內容。仍是 `REPLACE_ME` 時會被拒絕。 |
| `TAILSCALE_AUTH_KEY_FILE` | 本機 | 否 | 只含 auth key 的檔案路徑（不是 key 本身）。工具會以目前使用者讀取，複製到 `0700` 私有目錄中的 `0600` 暫存檔，再以 `file:` 交給 Tailscale；指令結束後刪除。同一來源內優先於 `TAILSCALE_AUTH_KEY`。 |
| `TAILSCALE_LOGIN_SERVER` | 控制平面 | 否 | 自訂控制平面 URL，例如 Headscale。有值時傳入 `--login-server`；留空使用官方 Tailscale。 |
| `TAILSCALE_HOSTNAME` | 本機 | 否 | 註冊到 tailnet 的 hostname。有值時傳入 `--hostname`；留空使用作業系統 hostname。 |
| `TAILSCALE_ADVERTISE_TAGS` | 本機 | 否 | 逗號分隔的 tags，例如 `tag:dev,tag:linux`。有值時傳入 `--advertise-tags`。 |
| `TAILSCALE_ACCEPT_DNS` | 本機 | 否 | `true` 或 `false`，轉成 `--accept-dns=true` 或 `--accept-dns=false`。留空則使用 Tailscale 預設值。 |
| `TAILSCALE_REMOTE_SSH_USER` | 遠端 peer | 否 | `ssh`／`web` 登入遠端 peer 時使用的 SSH 使用者。CLI `--remote-user` 優先；未設定時 TTY 詢問。 |
| `TAILSCALE_REMOTE_WEB_PORT` | 遠端 peer | 否 | `web` 未指定 `--port` 時使用的遠端 port，預設 `80`。 |

`TAILSCALE_REMOTE_SSH_USER` 是連到遠端 peer 時使用的登入帳號，不是別人連進本機的 SSH 使用者。

`TAILSCALE_AUTH_KEY_FILE` 與 CLI 的關係：

- `--auth-key`：直接提供 inline key。
- `--auth-key-file PATH`：使用指定檔案，優先於 env 檔設定。
- 跨來源優先序為 CLI > shell 環境變數 > env 檔；同一來源內，`TAILSCALE_AUTH_KEY_FILE` 優先於 `TAILSCALE_AUTH_KEY`。

## 全域選項

```text
--env-file PATH       指定 env 檔（預設：程式目錄下的 .tailscale.env）
--sudo                以 sudo 執行 tailscale 或 systemctl
--no-sudo             不使用 sudo
--no-start-daemon     只偵測 daemon，不嘗試啟動 tailscaled
--debug               顯示實際執行的指令（auth key 會遮蔽）
```

未指定 `--sudo`／`--no-sudo` 時，唯讀操作預設不使用 sudo；`join` 與啟動 daemon 等寫入操作在非 root 使用者下會嘗試使用 sudo。

## 指令

直接執行 `./tailnetctl.py` 會顯示全域選項、所有指令與可用參數摘要；加上 `<command> --help` 可查看單一指令的完整說明。

### `status` 與 `list` 的差別

| 指令 | 輸出內容 | 選項 |
| --- | --- | --- |
| `status` | tailnet／本機摘要與 peer 數量 | `--json` |
| `list` | 所有裝置清單（含自己） | `--json`、`--online-only` |

要看本機與 tailnet 整體狀態時使用 `status`；要查看或篩選裝置時使用 `list`。`ssh`／`web` 的互動選單也使用與 `list` 相同的裝置順序（含自己）。

### `join`

```bash
./tailnetctl.py join [--auth-key KEY] [--auth-key-file PATH] [--force] [--reset] [--dry-run]
```

`join` 會執行 `tailscale up`，不是 `tailscale login`。

- 已連線且 `BackendState=Running` 時，預設只顯示目前狀態，不會重新認證；若 self 離線，會提示確認網路或使用 `--force`。
- 要重新認證或切換 tailnet，加上 `--force`；這可能中斷目前連線。
- `--reset` 會傳入 `tailscale up --reset`。
- `--dry-run` 只顯示指令，auth key 顯示為 `<redacted>`；若會使用 sudo，指令也會顯示 sudo。
- `--dry-run --force` 會在指令前顯示重新認證警告。
- 已 `Running` 且未加 `--force` 時，即使 `--dry-run` 也只顯示目前狀態。
- 執行後會輪詢 `tailscale status --json`，最多等待約 60 秒確認進入 `Running` 且 self 在線。

範例：

```bash
./tailnetctl.py join --dry-run
./tailnetctl.py join
./tailnetctl.py join --force
```

### `status`

```bash
./tailnetctl.py status [--json]
```

顯示 tailnet 名稱、MagicDNS suffix、本機 hostname、Tailscale IP、`BackendState`、`Online`，以及 peer 總數與 online 數量，不列出 peer 表格。

- `--json` 輸出原始 Tailscale status JSON（進階除錯用途）。
- 若 `BackendState` 是 `NeedsLogin`，會提示先執行 `join`；其他非 `Running` 狀態會提示目前未連線。
- 若 `BackendState=Running` 但 `Self.Online=false`，會提示目前顯示的是快取狀態。
- 若要看裝置清單，使用 `list`。

### `list`

```bash
./tailnetctl.py list [--json] [--online-only]
```

列出所有裝置（含自己），不輸出 tailnet／本機摘要。自己固定第一列並標示 `(self)`；欄位包含序號、hostname、OS、online 狀態、Tailscale IP、owner 與 tags。

- `--online-only` 只列出 online 的裝置。
- `--json` 輸出裝置陣列，第一筆是自己且 `self` 為 `true`，適合搭配其他工具。
- 尚未連線或本機離線時會顯示警告；peer 資訊可能是快取。

### `ssh`

```bash
./tailnetctl.py ssh [PEER] [--peer PEER] [--remote-user USER] \
  [--ssh-port N] [--ssh-arg ARG] [--dry-run]
```

- `BackendState` 不是 `Running`，或本機目前離線時，會直接報錯。
- 未指定裝置且為 TTY 時會進入互動選單；非 TTY 環境需指定 `PEER` 或使用 `--peer`。
- 互動選單順序與 `list` 相同，包含自己並標示 `(self)`。
- `--remote-user` 未指定且 `TAILSCALE_REMOTE_SSH_USER` 為空時，TTY 會詢問遠端登入使用者；Enter 使用 ssh 預設，`q` 取消。
- `PEER` 支援 hostname、DNSName、Tailscale IP、node ID、選單序號或自己。
- `PEER` 與 `--peer` 請擇一；同時給不同值會報錯。
- 優先使用 DNSName（去掉結尾 dot），找不到時退回第一個 IPv4。
- 使用 `os.execvp()` 交棒給系統 `ssh`，沿用 `~/.ssh/config`、agent 與 `known_hosts`。
- 使用系統原生 `ssh`，因此 `known_hosts` 與 host key 檢查照常生效；不自動關閉 `StrictHostKeyChecking`。
- 主機參數前會加上 `--`，且遠端使用者不可 `-` 開頭，避免參數注入。
- 節點重建後若出現 `REMOTE HOST IDENTIFICATION HAS CHANGED`，先確認是預期變更，再移除舊記錄：

```bash
ssh-keygen -R <hostname>
ssh-keygen -R <ip>
```

- 若目標已啟用 Tailscale SSH，可手動執行：

```bash
tailscale ssh user@host
```

- `web` 仍使用原生 `ssh`，不整合 `tailscale ssh` wrapper。
- `--ssh-arg` 可重複，例如：

```bash
./tailnetctl.py ssh --peer peer-host --ssh-arg='-o StrictHostKeyChecking=accept-new'
```

### `web`

```bash
./tailnetctl.py web [PEER] [--peer PEER] [--port PORT] \
  [--local-port N] [--bind ADDR] [--remote-host HOST] \
  [--remote-user USER] [--dry-run]
```

建立 SSH 本地端口轉送：

```text
ssh -N -o ExitOnForwardFailure=yes \
    -L <bind>:<local-port>:<remote-host>:<remote-port> -- <peer>
```

- `BackendState` 不是 `Running`，或本機目前離線時，會直接報錯。
- `PEER` 與 `--peer` 請擇一；同時給不同值會報錯。
- 預設 `bind=127.0.0.1`、`remote-host=127.0.0.1`、`local-port=8080`。
- `--port` 未指定時使用 `TAILSCALE_REMOTE_WEB_PORT`，再退回 `80`。
- `--remote-user`／`TAILSCALE_REMOTE_SSH_USER` 的互動詢問行為與 `ssh` 相同。
- 執行前會依 `--bind` 顯示本機 URL；IPv6 會加方括號，例如 `--bind ::1` 顯示 `http://[::1]:8080/`。
- `--bind 0.0.0.0`／`::` 會顯示 loopback URL，並警告其他機器可連入本機 port。

完整的 A／B 情境、流量圖與更多範例請見 [docs/web-usage.md](docs/web-usage.md)。

範例：

```bash
./tailnetctl.py web --peer peer-host --port 80
./tailnetctl.py web --peer peer-host --port 80 --local-port 9090
./tailnetctl.py web --peer peer-host --port 80 --remote-user root
./tailnetctl.py web --peer 100.64.0.10 --port 80 --dry-run
```

## Daemon 偵測

1. 以 `shutil.which()` 尋找 `tailscale` client；找不到時顯示安裝指引。
2. 執行 `tailscale status --json` 判斷 daemon 是否可用。
3. daemon 不可用時：
   - 有 systemd：嘗試 `systemctl start tailscaled`，最多輪詢約 10 秒。
   - 沒有 systemd：才尋找 `tailscaled`；存在時印出手動啟動指令，缺少時顯示安裝指引。
4. 使用 `--no-start-daemon` 可停用自動啟動。

容器或外部 socket 環境可能只有 `tailscale` client；這種情況下不會要求本機必須有 `tailscaled`。

## 安全性

- auth key 不會被列印；`--dry-run` 與 `--debug` 都會遮蔽。
- inline 或 `TAILSCALE_AUTH_KEY_FILE` 的 key 會複製到 `0700` 私有目錄中的 `0600` 暫存檔，指令結束後刪除。
- 使用 `TAILSCALE_AUTH_KEY_FILE` 時，該檔案應僅包含 key，並建議設為 `0600`。
- `.tailscale.env` 權限若允許群組或其他使用者讀取，程式會警告並建議 `chmod 600`。
- 所有外部指令都以參數陣列執行，不使用 `shell=True`。
- `.tailscale.env` 已列入 `.gitignore`，避免誤提交金鑰。

## 測試

```bash
python3 -m unittest discover -s tests -v
```

測試不會呼叫實際的 `tailscale up`、SSH 或端口轉送。

## 常見問題

**找不到 auth key**

程式會顯示建立 `.tailscale.env` 的指令。請先至 [Auth keys](https://console.tailscale.com/admin/settings/keys) 建立 key，填入有效的 `TAILSCALE_AUTH_KEY`，或改用 `--auth-key`／`--auth-key-file`。

**auth key 仍是 placeholder**

`REPLACE_ME` 會被視為未設定。請換成有效的 key。

**TAILSCALE_AUTH_KEY_FILE 填成 key 本身**

這個變數只接受檔案路徑，不是 key。請把 key 放在 `TAILSCALE_AUTH_KEY`，或把 key 寫入檔案後，在 `TAILSCALE_AUTH_KEY_FILE` 填入該檔案路徑。

**找不到 tailscale**

Linux 可執行：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

其他平台請從 <https://tailscale.com/download> 下載安裝。

**BackendState 是 NeedsLogin**

表示目前未登入 tailnet。執行 `./tailnetctl.py join` 重新加入；必要時加上 `--force`。

**BackendState 是 Running 但 Online 是 no**

本機目前離線，`status`／`list` 顯示的是本機 daemon 的快取資料，後台變更可能尚未同步。請確認網路後重新連線：

```bash
sudo systemctl restart tailscaled
# 或
sudo tailscale down && sudo tailscale up
```

若本機就是剛從後台移除的 machine，重新加入需執行 `./tailnetctl.py join`；若要維持移除狀態，請不要 join。

**非互動環境無法選擇裝置**

在腳本或無 TTY 的環境請指定 `PEER` 或加上 `--peer`。

**SSH 顯示 REMOTE HOST IDENTIFICATION HAS CHANGED**

通常是節點重建後 host key 改變。先確認這是預期變更，再移除舊記錄：

```bash
ssh-keygen -R <hostname>
ssh-keygen -R <ip>
```

不要常態使用 `StrictHostKeyChecking=no`。

**Tailscale SSH 要怎麼用**

若目標已啟用 Tailscale SSH，可直接手動執行：

```bash
tailscale ssh user@host
```

本工具的 `ssh`／`web` 仍使用系統原生 `ssh`；`web` 不整合 `tailscale ssh`，因為其參數順序不適合傳遞 `-L`／`-N`。

**無法連線到 tailscaled**

確認 `tailscaled` 是否執行。若使用 systemd，可執行 `sudo systemctl start tailscaled`；否則依程式提示手動啟動，或使用 `--no-start-daemon` 只做偵測。
