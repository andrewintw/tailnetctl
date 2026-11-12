# `tailnetctl.py web` 使用說明

[回到 README](../README.md)

## 角色定義

- **Server A**：你正在操作的機器，也是執行 `tailnetctl.py`、開瀏覽器的地方。
- **Server B**：Tailscale 上的另一台 peer，提供你要存取的網頁服務。
- 假設 B 的網站跑在 port `80`，而且你可以用 SSH 登入 B。

本工具需在 POSIX 系統（Linux／macOS）執行；Windows 請使用 WSL。

## 一句話說明

`tailnetctl.py web` 會在本機 A 開一個 port，並透過 SSH tunnel 把流量送到 B 的指定 port。

## 基本指令

在 A 執行：

```bash
./tailnetctl.py web --peer serverB --port 80
```

成功後，A 的瀏覽器開啟：

```text
http://127.0.0.1:8080/
```

實際上看到的是 B 的 port `80`。

## 等價的 SSH 指令

`tailnetctl.py web` 本質上會執行類似以下的指令：

```bash
ssh -N -o ExitOnForwardFailure=yes \
    -L 127.0.0.1:8080:127.0.0.1:80 -- user@serverB
```

各部分意義：

- `-N`：不執行遠端命令，只做端口轉送。
- `ExitOnForwardFailure=yes`：本機 port 無法綁定時直接失敗，不要假裝成功。
- `127.0.0.1:8080`：在 A 監聽的本機位址與 port。
- `127.0.0.1:80`：從 B 的角度要連線的目標位址與 port。
- `user@serverB`：登入 B 的 SSH 使用者與主機。

## 流量圖

```text
[A] 瀏覽器
  |
  | http://127.0.0.1:8080
  v
[A] SSH client（tailnetctl.py web）
  |
  | SSH tunnel，經過 Tailscale
  v
[B] SSH server
  |
  | B 連到自己的 127.0.0.1:80
  v
[B] 網頁服務 :80
```

一行表示：

```text
A 瀏覽器 -> A:127.0.0.1:8080 -> SSH tunnel -> B:127.0.0.1:80 -> B 的網站
```

## 參數對應

| 參數 | 作用位置 | 預設 | 說明 |
| --- | --- | --- | --- |
| `PEER` / `--peer serverB` | B | TTY 時顯示選單 | 要 SSH 進去的機器 |
| `--port 80` | B | `TAILSCALE_REMOTE_WEB_PORT`，再退回 `80` | B 上要存取的 port |
| `--local-port 8080` | A | `8080` | A 本機開放的 port |
| `--bind 127.0.0.1` | A | `127.0.0.1` | A 監聽的位址 |
| `--remote-host 127.0.0.1` | 從 B 的視角 | `127.0.0.1` | B 要連去哪台主機 |
| `--remote-user` | B | TTY 時詢問 | 登入 B 的 SSH 使用者 |

## 常見用法

### 1. B 的網站是 port 80

```bash
./tailnetctl.py web --peer serverB --port 80
```

A 開啟：

```text
http://127.0.0.1:8080/
```

### 2. B 的網站是 port 3000

```bash
./tailnetctl.py web --peer serverB --port 3000
```

A 開啟：

```text
http://127.0.0.1:8080/
```

### 3. A 的 8080 已被占用，改用 9090

```bash
./tailnetctl.py web --peer serverB --port 80 --local-port 9090
```

A 開啟：

```text
http://127.0.0.1:9090/
```

### 4. 網站不在 B，而是在只有 B 連得到的 C

假設 C 是 `192.0.2.50`，網站 port 是 `8080`：

```bash
./tailnetctl.py web --peer serverB \
  --remote-host 192.0.2.50 --port 8080
```

流量會是：

```text
A:8080 -> SSH -> B -> C:8080
```

### 5. 指定登入 B 的使用者

```bash
./tailnetctl.py web --peer serverB --port 80 --remote-user ubuntu
```

如果沒有指定 `--remote-user`，且 `TAILSCALE_REMOTE_SSH_USER` 為空，TTY 會詢問你要登入 B 的哪個帳號。直接按 Enter 代表交給 ssh 預設；輸入 `q` 取消。

### 6. 不指定 peer，使用互動選單

```bash
./tailnetctl.py web --port 80
```

TTY 會先讓你選裝置，再視需要詢問遠端 SSH 使用者。

### 7. 只顯示指令，不實際連線

```bash
./tailnetctl.py web --peer serverB --port 80 --dry-run
```

## SSH host key 與 Tailscale SSH

`web` 使用系統原生 `ssh`，因此會檢查 `known_hosts` 與 host key。節點重建後若出現：

```text
REMOTE HOST IDENTIFICATION HAS CHANGED
```

請先確認這是預期變更，再移除舊記錄：

```bash
ssh-keygen -R <hostname>
ssh-keygen -R <ip>
```

不要常態使用 `StrictHostKeyChecking=no`。

若目標已啟用 Tailscale SSH，可手動執行：

```bash
tailscale ssh user@host
```

`tailscale ssh` 是 system ssh 的 wrapper，會使用 coordination server 提供的 host key；但它的參數格式是 `tailscale ssh [user@]host [args...]`，不適合直接傳入 `-L`／`-N`。因此 `web` 仍使用原生 `ssh`，不整合 `tailscale ssh`。

## 重要觀念

- `--port` 是**遠端** port，不是 A 的 port。
- `--local-port` 才是**A 瀏覽器要開的 port**。
- `--remote-host` 是**從 B 的角度**要連的主機。
- `--remote-user`／`TAILSCALE_REMOTE_SSH_USER` 是登入 B 的帳號，不是別人連進 A 的帳號。
- `--remote-user`／`TAILSCALE_REMOTE_SSH_USER` 不可以 `-` 開頭；peer 位址若以 `-` 開頭也會被拒絕。
- SSH 指令會在 host 前加上 `--`，避免 host／user 被解析成 SSH 選項。
- `PEER` 與 `--peer` 請擇一；同時給不同值會報錯。
- `--bind 127.0.0.1` 只有 A 自己能存取；改成 `0.0.0.0` 會開放其他機器連入 A。
- 輸出 URL 會依 `--bind` 變動；IPv6 會加方括號，例如 `::1` 顯示 `http://[::1]:8080/`。
- 使用 `--bind 0.0.0.0` 或 `::` 時，程式顯示的是 loopback URL；其他機器要改用 A 的 IP。
- 這個指令不會啟動網頁伺服器，只建立 SSH tunnel。
- 指令會持續執行，按 `Ctrl-C` 結束轉送。
- 本機 Tailscale 必須是 `Running` 且 online。
- 非 TTY 環境選擇 peer 時需提供 `PEER` 或 `--peer`；遠端使用者則直接交給 ssh 預設。
- 建議先確認 SSH 可以登入 B：

```bash
./tailnetctl.py ssh --peer serverB
```

- 如果 B 的網站是 HTTPS，瀏覽器要用 `https://127.0.0.1:<local-port>/`，不是 `http://`。
