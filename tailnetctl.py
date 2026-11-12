#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".tailscale.env"
ENV_EXAMPLE_FILE = SCRIPT_DIR / ".tailscale.env.example"
DEFAULT_LOCAL_PORT = 8080
DEFAULT_BIND = "127.0.0.1"
DEFAULT_REMOTE_HOST = "127.0.0.1"
DEFAULT_SSH_PORT = 22
DEFAULT_WEB_PORT = 80
START_WAIT_SECONDS = 10
JOIN_WAIT_SECONDS = 60


class CurrentTailnet(TypedDict, total=False):
    Name: str
    MagicDNSSuffix: str


class TailscaleUser(TypedDict, total=False):
    LoginName: str
    DisplayName: str


class TailscaleNode(TypedDict, total=False):
    ID: str
    HostName: str
    DNSName: str
    OS: str
    Online: bool
    InMagicSock: bool
    TailscaleIPs: List[str]
    UserID: Union[str, int]
    Tags: Optional[List[str]]
    Self: bool


class TailscaleStatus(TypedDict, total=False):
    BackendState: str
    CurrentTailnet: Optional[CurrentTailnet]
    Self: TailscaleNode
    Peer: Union[Dict[str, TailscaleNode], List[TailscaleNode]]
    User: Dict[str, TailscaleUser]


AuthKeySpec = Tuple[Literal["file", "inline"], str]


class CtlError(Exception):
    pass


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def ensure_posix_supported(os_name: Optional[str] = None) -> None:
    name = os_name or os.name
    if name != "posix":
        raise CtlError(
            "此工具目前僅支援 POSIX 系統（Linux/macOS）；"
            "Windows 請使用 WSL。"
        )


def safe_text(value: Optional[str]) -> str:
    return re.sub(r"tskey-[A-Za-z0-9_-]+", "<redacted>", value or "")


def redact_arg(value: str) -> str:
    if value.startswith("--auth-key="):
        return "--auth-key=<redacted>"
    if "tskey-" in value:
        return "<redacted>"
    return value


def format_command(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(redact_arg(str(item))) for item in argv)


def check_env_permissions(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        eprint(
            f"警告: {path} 權限過寬，建議執行: "
            f"chmod 600 {shlex.quote(str(path))}"
        )


def strip_inline_comment(value: str) -> str:
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def unquote_env_value(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
        return value

    inner = value[1:-1]
    if value[0] == "'":
        return inner

    result: List[str] = []
    escaped = False
    for char in inner:
        if escaped:
            if char in ('"', "\\"):
                result.append(char)
            else:
                result.append("\\")
                result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    content = path.read_text(encoding="utf-8-sig")
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_inline_comment(value).strip()
        if not key:
            continue
        values[key] = unquote_env_value(value)
    return values


class Config:
    def __init__(
        self,
        args: argparse.Namespace,
        env_path: Path,
        env_values: Dict[str, str],
        env_exists: bool,
    ) -> None:
        self.args = args
        self.env_path = env_path
        self.env_values = env_values
        self.env_exists = env_exists

    @staticmethod
    def shell_get(name: str) -> Optional[str]:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
        return None

    def file_get(self, name: str) -> Optional[str]:
        value = self.env_values.get(name)
        if value is not None and value.strip():
            return value.strip()
        return None

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = self.shell_get(name)
        if value is not None:
            return value
        value = self.file_get(name)
        if value is not None:
            return value
        return default

    @property
    def remote_web_port(self) -> int:
        value = self.get("TAILSCALE_REMOTE_WEB_PORT")
        if not value:
            return DEFAULT_WEB_PORT
        try:
            port = int(value)
        except ValueError:
            return DEFAULT_WEB_PORT
        return port if 1 <= port <= 65535 else DEFAULT_WEB_PORT


def load_config(args: argparse.Namespace) -> Config:
    env_arg = getattr(args, "env_file", None)
    explicit = env_arg is not None
    env_path = Path(env_arg).expanduser() if explicit else DEFAULT_ENV_FILE
    if env_path.exists():
        if not env_path.is_file():
            raise CtlError(f"env 路徑不是檔案: {env_path}")
        check_env_permissions(env_path)
        try:
            values = parse_env_file(env_path)
        except OSError as exc:
            raise CtlError(f"無法讀取 env 檔 {env_path}: {exc}")
    elif explicit:
        raise CtlError(f"找不到 env 檔: {env_path}")
    else:
        values = {}
    return Config(args, env_path, values, env_path.exists())


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.debug = bool(getattr(args, "debug", False))
        self.sudo: Optional[bool] = getattr(args, "sudo", None)

    def maybe_sudo(self, argv: Sequence[str], mutating: bool) -> List[str]:
        use_sudo = self.sudo
        if use_sudo is None:
            use_sudo = mutating and os.geteuid() != 0
        if use_sudo and os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                raise CtlError("需要 sudo 但找不到 sudo 指令（可加上 --no-sudo）")
            return [sudo] + list(argv)
        return list(argv)

    def run(
        self,
        argv: Sequence[str],
        mutating: bool = False,
        capture: bool = True,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        full = self.maybe_sudo(argv, mutating)
        if self.debug:
            eprint("+ " + format_command(full))
        try:
            return subprocess.run(
                full, capture_output=capture, text=True, timeout=timeout
            )
        except FileNotFoundError:
            raise CtlError(f"找不到指令: {full[0]}")


def install_hint(platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return (
            "Linux 安裝指令:\n"
            "  curl -fsSL https://tailscale.com/install.sh | sh"
        )
    return "下載: https://tailscale.com/download"


def missing_tailscale_message(
    binary: str = "tailscale", platform: Optional[str] = None
) -> str:
    return f"找不到 {binary}，請先安裝 Tailscale。\n{install_hint(platform)}"


def require_tailscale() -> str:
    binary = shutil.which("tailscale")
    if not binary:
        raise CtlError(missing_tailscale_message())
    return binary


def try_status(
    ctx: Ctx, tailscale: str
) -> Tuple[Optional[TailscaleStatus], Optional[str]]:
    try:
        proc = ctx.run([tailscale, "status", "--json"], timeout=20)
    except subprocess.TimeoutExpired:
        return None, "tailscale status 逾時"
    if proc.returncode != 0:
        message = safe_text((proc.stderr or proc.stdout or "").strip())
        return None, message or f"tailscale status 失敗（exit {proc.returncode}）"
    try:
        return json.loads(proc.stdout or "{}"), None
    except json.JSONDecodeError as exc:
        return None, f"無法解析 tailscale status JSON: {exc}"


def try_start_daemon(ctx: Ctx) -> bool:
    systemctl = shutil.which("systemctl")
    if systemctl and Path("/run/systemd/system").is_dir():
        eprint("tailscaled 似乎未執行，嘗試透過 systemd 啟動...")
        proc = ctx.run([systemctl, "start", "tailscaled"], mutating=True)
        if proc.returncode == 0:
            return True
        message = safe_text((proc.stderr or proc.stdout or "").strip())
        if message:
            eprint(message)
        eprint("systemd 啟動 tailscaled 失敗。")
        return False

    tailscaled = shutil.which("tailscaled")
    if not tailscaled:
        eprint(missing_tailscale_message("tailscaled"))
        return False

    prefix = []
    if os.geteuid() != 0 and ctx.sudo is not False:
        sudo = shutil.which("sudo")
        if sudo:
            prefix = [sudo]
    command = prefix + [
        tailscaled,
        "--state=/var/lib/tailscale/tailscaled.state",
        "--socket=/var/run/tailscale/tailscaled.sock",
    ]
    eprint("找不到可用的 systemd，請手動啟動 tailscaled，例如：")
    eprint("  " + format_command(command))
    return False


def get_status(
    ctx: Ctx, tailscale: str, allow_start: bool = True
) -> TailscaleStatus:
    data, error = try_status(ctx, tailscale)
    if data is not None:
        return data
    can_start = "permission denied" not in (error or "").lower()
    if allow_start and can_start and getattr(ctx.args, "start_daemon", True):
        if try_start_daemon(ctx):
            deadline = time.monotonic() + START_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.5)
                data, error = try_status(ctx, tailscale)
                if data is not None:
                    return data
    raise CtlError(f"無法取得 tailscale 狀態：{error}")


def is_placeholder(value: Optional[str]) -> bool:
    value = (value or "").strip()
    return not value or "REPLACE_ME" in value


def missing_key_message(env_path: Path) -> str:
    return (
        "找不到可用的 auth key。請建立 env 檔：\n"
        f"  cp {shlex.quote(str(ENV_EXAMPLE_FILE))} "
        f"{shlex.quote(str(env_path))}\n"
        f"  chmod 600 {shlex.quote(str(env_path))}\n"
        "然後填入 TAILSCALE_AUTH_KEY，或使用 --auth-key / --auth-key-file。"
    )


def validate_key_file(path: Path) -> None:
    if not path.is_file():
        raise CtlError(f"找不到 auth key 檔案: {safe_text(str(path))}")
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CtlError(f"無法讀取 auth key 檔案 {path}: {exc}")
    if is_placeholder(content):
        raise CtlError("auth key 檔案內容為空或仍是 placeholder。")


def resolve_auth_key(
    args: argparse.Namespace, cfg: Config
) -> AuthKeySpec:
    candidates: List[Tuple[Literal["file", "inline"], str]] = []
    if args.auth_key_file:
        candidates.append(("file", args.auth_key_file))
    if args.auth_key:
        candidates.append(("key", args.auth_key))
    shell_key_file = cfg.shell_get("TAILSCALE_AUTH_KEY_FILE")
    if shell_key_file:
        candidates.append(("file", shell_key_file))
    shell_key = cfg.shell_get("TAILSCALE_AUTH_KEY")
    if shell_key:
        candidates.append(("key", shell_key))
    file_key_file = cfg.file_get("TAILSCALE_AUTH_KEY_FILE")
    if file_key_file:
        candidates.append(("file", file_key_file))
    file_key = cfg.file_get("TAILSCALE_AUTH_KEY")
    if file_key:
        candidates.append(("key", file_key))

    if not candidates:
        raise CtlError(missing_key_message(cfg.env_path))

    kind, value = candidates[0]
    if kind == "file":
        if value.startswith("tskey-"):
            raise CtlError(
                "TAILSCALE_AUTH_KEY_FILE 必須是檔案路徑，不是 auth key；"
                "請將 key 放在 TAILSCALE_AUTH_KEY，或寫入檔案後填入檔案路徑。"
            )
        path = Path(value).expanduser()
        validate_key_file(path)
        return ("file", str(path))

    value = value.strip()
    if value.startswith("file:"):
        path = Path(value[5:]).expanduser()
        validate_key_file(path)
        return ("file", str(path))

    if is_placeholder(value):
        raise CtlError(missing_key_message(cfg.env_path))
    return ("inline", value)


def materialize_key(key: AuthKeySpec) -> Tuple[str, Optional[Path]]:
    kind, value = key
    if kind == "file":
        source = Path(value)
        try:
            content = source.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CtlError(
                f"無法讀取 auth key 檔案 {safe_text(str(source))}: {exc}"
            )
    else:
        content = value

    temp_dir = Path(tempfile.mkdtemp(prefix="tailnetctl-"))
    fd = -1
    key_path: Optional[Path] = None
    try:
        os.chmod(temp_dir, 0o700)
        fd, name = tempfile.mkstemp(
            prefix="auth-", suffix=".key", dir=str(temp_dir)
        )
        key_path = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content + "\n")
        fd = -1
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return f"--auth-key=file:{key_path}", temp_dir


def build_up_argv(
    tailscale: str,
    key_arg: str,
    cfg: Config,
    args: argparse.Namespace,
) -> List[str]:
    argv = [tailscale, "up", key_arg]

    login_server = cfg.get("TAILSCALE_LOGIN_SERVER")
    if login_server:
        argv += ["--login-server", login_server]

    hostname = cfg.get("TAILSCALE_HOSTNAME")
    if hostname:
        argv += ["--hostname", hostname]

    tags = cfg.get("TAILSCALE_ADVERTISE_TAGS")
    if tags:
        argv += ["--advertise-tags", tags]

    accept_dns = cfg.get("TAILSCALE_ACCEPT_DNS")
    if accept_dns:
        normalized = accept_dns.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            argv.append("--accept-dns=true")
        elif normalized in ("false", "0", "no", "off"):
            argv.append("--accept-dns=false")
        else:
            raise CtlError("TAILSCALE_ACCEPT_DNS 必須是 true 或 false")

    if args.force:
        argv.append("--force-reauth")
    if args.reset:
        argv.append("--reset")

    return argv


def wait_for_online(ctx: Ctx, tailscale: str) -> TailscaleStatus:
    deadline = time.monotonic() + JOIN_WAIT_SECONDS
    last_state = None
    while time.monotonic() < deadline:
        data, error = try_status(ctx, tailscale)
        if data is not None:
            state = data.get("BackendState")
            online = self_is_online(data)
            if state == "Running" and online:
                return data
            last_state = (
                f"BackendState={state or 'unknown'}, "
                f"Online={'yes' if online else 'no'}"
            )
        else:
            last_state = error
        time.sleep(1)
    raise CtlError(f"等待 tailnet 連線逾時（{last_state or '未知狀態'}）")


def first_ip(node: TailscaleNode) -> Optional[str]:
    ips = node.get("TailscaleIPs") or []
    if not isinstance(ips, list):
        return None
    for ip in ips:
        if isinstance(ip, str) and ":" not in ip:
            return ip
    for ip in ips:
        if isinstance(ip, str):
            return ip
    return None


def primary_ip(node: TailscaleNode) -> str:
    return first_ip(node) or "-"


def tailnet_info(data: TailscaleStatus) -> Tuple[str, str]:
    current = data.get("CurrentTailnet") or {}
    if not isinstance(current, dict):
        current = {}
    name = current.get("Name") or "-"
    magic = current.get("MagicDNSSuffix") or name
    return name, magic


def owner_map(data: TailscaleStatus) -> Dict[str, str]:
    users: Dict[str, str] = {}
    raw = data.get("User") or {}
    if isinstance(raw, dict):
        for user_id, user in raw.items():
            if isinstance(user, dict):
                users[str(user_id)] = (
                    user.get("LoginName")
                    or user.get("DisplayName")
                    or str(user_id)
                )
    return users


def owner_name(users: Mapping[str, str], peer: TailscaleNode) -> str:
    return users.get(str(peer.get("UserID") or "")) or "-"


def collect_peers(data: TailscaleStatus) -> List[TailscaleNode]:
    raw = data.get("Peer") or {}
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(str(peer.get("ID", "")), peer) for peer in raw if isinstance(peer, dict)]
    else:
        items = []

    peers = []
    for key, peer in items:
        if not isinstance(peer, dict):
            continue
        item = dict(peer)
        item.setdefault("ID", key)
        peers.append(item)

    peers.sort(
        key=lambda peer: (
            not bool(peer.get("Online")),
            str(peer.get("HostName") or peer.get("DNSName") or "").lower(),
        )
    )
    return peers


def self_is_online(data: TailscaleStatus) -> bool:
    self_node = data.get("Self")
    if not isinstance(self_node, dict):
        return False
    online = self_node.get("Online")
    if online is not None:
        return bool(online)
    return data.get("BackendState") == "Running"


def collect_devices(data: TailscaleStatus) -> List[TailscaleNode]:
    devices: List[TailscaleNode] = []
    self_node = data.get("Self")
    if isinstance(self_node, dict):
        item = dict(self_node)
        item["Online"] = self_is_online(data)
        item["Self"] = True
        devices.append(item)

    for peer in collect_peers(data):
        item = dict(peer)
        item["Self"] = False
        devices.append(item)
    return devices


def format_table(
    headers: Sequence[str], rows: Sequence[Sequence[object]]
) -> str:
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def render(row):
        return "  ".join(
            str(cell).ljust(widths[index]) for index, cell in enumerate(row)
        ).rstrip()

    lines = [render(headers), "  ".join("-" * width for width in widths)]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


def peer_table(
    data: TailscaleStatus,
    peers: Sequence[TailscaleNode],
    include_tags: bool = True,
) -> str:
    users = owner_map(data)
    headers = ["#", "HOSTNAME", "OS", "ONLINE", "IP", "OWNER"]
    if include_tags:
        headers.append("TAGS")

    rows = []
    for index, peer in enumerate(peers, 1):
        hostname = peer.get("HostName") or "-"
        if peer.get("Self"):
            hostname = f"{hostname} (self)"
        row = [
            index,
            hostname,
            peer.get("OS") or "-",
            "yes" if peer.get("Online") else "no",
            primary_ip(peer),
            owner_name(users, peer),
        ]
        if include_tags:
            tags = peer.get("Tags") or []
            row.append(",".join(str(tag) for tag in tags) if tags else "-")
        rows.append(row)
    return format_table(headers, rows)


def print_connection(data: TailscaleStatus) -> None:
    name, _ = tailnet_info(data)
    self_node = data.get("Self") or {}
    print(f"Tailnet:      {name}")
    print(f"Hostname:     {self_node.get('HostName') or '-'}")
    print(f"Tailscale IP: {primary_ip(self_node)}")
    print(f"BackendState: {data.get('BackendState') or '-'}")
    print(f"Online:       {'yes' if self_is_online(data) else 'no'}")


def print_status(data: TailscaleStatus) -> None:
    name, magic = tailnet_info(data)
    self_node = data.get("Self") or {}
    peers = collect_peers(data)
    online_peers = sum(1 for peer in peers if peer.get("Online"))
    state = data.get("BackendState") or "-"
    online = self_is_online(data)
    print(f"Tailnet:      {name}")
    print(f"MagicDNS:     {magic}")
    print(f"Hostname:     {self_node.get('HostName') or '-'}")
    print(f"Tailscale IP: {primary_ip(self_node)}")
    print(f"BackendState: {state}")
    print(f"Online:       {'yes' if online else 'no'}")
    print(f"Peers:        {len(peers)} total, {online_peers} online")
    if state == "NeedsLogin":
        print()
        print("提示: 尚未登入 tailnet；請執行 join 加入。")
    elif state != "Running":
        print()
        print(f"提示: 目前未連線到 tailnet（{state}）。")
    elif not online:
        print()
        print("提示: 本機目前離線；顯示的是快取狀態，peer 變更可能尚未同步。")


def peer_to_dict(peer: TailscaleNode, users: Mapping[str, str]) -> Dict[str, Any]:
    return {
        "id": peer.get("ID") or None,
        "hostname": peer.get("HostName"),
        "dns_name": (peer.get("DNSName") or "").rstrip(".") or None,
        "os": peer.get("OS"),
        "online": bool(peer.get("Online")),
        "ip": first_ip(peer),
        "ips": peer.get("TailscaleIPs") or [],
        "owner": owner_name(users, peer),
        "tags": peer.get("Tags") or [],
        "self": bool(peer.get("Self")),
    }


def resolve_peer(
    query: str, peers: Sequence[TailscaleNode]
) -> Optional[TailscaleNode]:
    query = (query or "").strip()
    if not query:
        return None

    if query.isdigit():
        index = int(query)
        if 1 <= index <= len(peers):
            return peers[index - 1]

    lowered = query.lower().lstrip("@").rstrip(".")
    for peer in peers:
        names = {
            str(peer.get("HostName") or "").lower().rstrip("."),
            (peer.get("DNSName") or "").lower().rstrip("."),
            str(peer.get("ID") or "").lower(),
        }
        if lowered in names:
            return peer
        for ip in peer.get("TailscaleIPs") or []:
            if str(ip).lower() == lowered:
                return peer
    return None


def select_peer(
    data: TailscaleStatus, query: Optional[str]
) -> Optional[TailscaleNode]:
    devices = collect_devices(data)
    if not devices:
        raise CtlError("目前 tailnet 沒有可選裝置。")

    query = (query or "").strip()
    if query:
        peer = resolve_peer(query, devices)
        if peer is None:
            raise CtlError(f"找不到符合 {query!r} 的裝置。")
        return peer

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CtlError("非互動環境請指定 PEER 或加上 --peer。")

    print(peer_table(data, devices, include_tags=False))
    while True:
        try:
            choice = input("\n選擇裝置（序號或名稱，q 離開）: ").strip()
        except EOFError:
            return None
        if choice.lower() in ("q", "quit", "exit"):
            return None
        if not choice:
            continue
        peer = resolve_peer(choice, devices)
        if peer is None:
            print("無效的選擇，請重新輸入。")
            continue
        return peer


def resolve_peer_query(args: argparse.Namespace) -> Optional[str]:
    positional_raw = args.peer
    option_raw = args.peer_opt
    if positional_raw is not None and not positional_raw.strip():
        raise CtlError("PEER／--peer 不可為空白。")
    if option_raw is not None and not option_raw.strip():
        raise CtlError("PEER／--peer 不可為空白。")

    positional = (positional_raw or "").strip()
    option = (option_raw or "").strip()
    if positional and option and positional != option:
        raise CtlError(
            "同時指定了位置參數 PEER 與 --peer，且兩者不同；"
            "請只保留一個。"
        )
    return option or positional or None


def strip_brackets(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def bracket_host(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host
    if ":" in host:
        return f"[{host}]"
    return host


def normalize_web_host(
    value: Optional[str], option_name: str, default: str
) -> str:
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise CtlError(f"{option_name} 不可為空白。")
    return value


def is_loopback_bind(bind: str) -> bool:
    return strip_brackets(bind) in ("127.0.0.1", "::1", "localhost")


def format_local_url(bind: str, local_port: int) -> str:
    normalized = strip_brackets(bind)
    if normalized in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif normalized == "::":
        host = "[::1]"
    elif normalized in ("127.0.0.1", "localhost"):
        host = "127.0.0.1"
    elif ":" in normalized:
        host = f"[{normalized}]"
    else:
        host = normalized
    return f"http://{host}:{local_port}/"


def format_forward_spec(
    bind: str, local_port: int, remote_host: str, port: int
) -> str:
    return (
        f"{bracket_host(bind)}:{local_port}:"
        f"{bracket_host(remote_host)}:{port}"
    )


def peer_target(peer: TailscaleNode) -> str:
    dns_name = str(peer.get("DNSName") or "").rstrip(".").strip()
    if dns_name:
        return dns_name
    ip = first_ip(peer)
    if ip:
        return ip
    hostname = peer.get("HostName")
    if hostname:
        return str(hostname)
    node_id = peer.get("ID")
    if node_id:
        return str(node_id)
    raise CtlError("無法判斷 peer 的連線位址。")


def exec_ssh(argv: Sequence[str]) -> NoReturn:
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        raise CtlError("找不到 ssh 指令。")
    except OSError as exc:
        raise CtlError(f"無法執行 ssh：{exc}")


def cmd_join(args: argparse.Namespace, cfg: Config, ctx: Ctx) -> int:
    tailscale = require_tailscale()

    if args.dry_run:
        data, _ = try_status(ctx, tailscale)
    else:
        data = get_status(ctx, tailscale)

    if data is not None and data.get("BackendState") == "Running" and not args.force:
        print_connection(data)
        if self_is_online(data):
            print("若要重新認證或切換 tailnet，請加上 --force。")
        else:
            print("提示: 本機目前離線；請確認網路，或加上 --force 重新認證／加入。")
        return 0

    key = resolve_auth_key(args, cfg)

    if args.force:
        eprint(
            "警告: --force 會重新認證，並可能切換 tailnet 或中斷目前連線。"
        )

    if args.dry_run:
        argv = build_up_argv(
            tailscale, "--auth-key=file:<redacted>", cfg, args
        )
        argv = ctx.maybe_sudo(argv, mutating=True)
        print(format_command(argv))
        return 0

    key_arg, temp_dir = materialize_key(key)
    try:
        argv = build_up_argv(tailscale, key_arg, cfg, args)
        proc = ctx.run(argv, mutating=True)
        if proc.returncode != 0:
            message = safe_text((proc.stderr or proc.stdout or "").strip())
            detail = f"：{message}" if message else ""
            raise CtlError(f"tailscale up 失敗（exit {proc.returncode}）{detail}")
        data = wait_for_online(ctx, tailscale)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    print_connection(data)
    return 0


def cmd_status(args: argparse.Namespace, cfg: Config, ctx: Ctx) -> int:
    tailscale = require_tailscale()
    data = get_status(ctx, tailscale)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_status(data)
    return 0


def cmd_list(args: argparse.Namespace, cfg: Config, ctx: Ctx) -> int:
    tailscale = require_tailscale()
    data = get_status(ctx, tailscale)
    state = data.get("BackendState")
    if state != "Running":
        eprint(
            f"警告: 目前未連線到 tailnet（BackendState={state or 'unknown'}）；"
            "顯示的資訊可能不完整。"
        )
    elif not self_is_online(data):
        eprint("警告: 本機目前離線；peer 資訊可能是快取。")
    devices = collect_devices(data)
    if args.online_only:
        devices = [device for device in devices if device.get("Online")]
    users = owner_map(data)

    if args.json:
        payload = [peer_to_dict(device, users) for device in devices]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif devices:
        print(peer_table(data, devices))
    else:
        print("沒有符合條件的裝置。")
    return 0


def require_tailnet_online(data: TailscaleStatus) -> None:
    state = data.get("BackendState")
    if state != "Running":
        raise CtlError(
            f"目前未連線到 tailnet（BackendState={state or 'unknown'}）。"
            "請先執行 join 加入 tailnet。"
        )
    if not self_is_online(data):
        raise CtlError("本機目前離線；請確認網路或重新連線後再試。")


def validate_ssh_user(user: str) -> None:
    if user.startswith("-"):
        raise CtlError("遠端 SSH 使用者不可以 - 開頭。")


def resolve_remote_ssh_user(
    args: argparse.Namespace,
    cfg: Config,
    is_tty: Optional[bool] = None,
    prompt: Optional[Callable[[str], str]] = None,
) -> Tuple[Optional[str], bool]:
    user = (
        args.remote_user
        or cfg.get("TAILSCALE_REMOTE_SSH_USER")
        or ""
    ).strip() or None
    if user:
        validate_ssh_user(user)
        return user, False

    if is_tty is None:
        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_tty:
        return None, False

    ask = prompt or input
    try:
        value = ask(
            "登入遠端主機的 SSH 使用者（Enter 使用 ssh 預設，q 取消）: "
        )
    except EOFError:
        return None, True

    value = (value or "").strip()
    if value.lower() in ("q", "quit", "exit"):
        return None, True
    if value:
        validate_ssh_user(value)
    return (value or None), False


def prepare_ssh_target(
    args: argparse.Namespace, cfg: Config, ctx: Ctx
) -> Tuple[Optional[str], bool]:
    tailscale = require_tailscale()
    data = get_status(ctx, tailscale)
    require_tailnet_online(data)
    peer = select_peer(data, resolve_peer_query(args))
    if peer is None:
        return None, True

    target = peer_target(peer)
    if target.startswith("-"):
        raise CtlError("peer 位址不可以 - 開頭。")
    user, cancelled = resolve_remote_ssh_user(args, cfg)
    if cancelled:
        return None, True
    host = f"{user}@{target}" if user else target
    return host, False


def cmd_ssh(args: argparse.Namespace, cfg: Config, ctx: Ctx) -> int:
    host, cancelled = prepare_ssh_target(args, cfg, ctx)
    if cancelled:
        return 0

    argv = ["ssh"]
    if args.ssh_port and args.ssh_port != DEFAULT_SSH_PORT:
        argv += ["-p", str(args.ssh_port)]
    for extra in args.ssh_arg or []:
        try:
            argv.extend(shlex.split(extra))
        except ValueError as exc:
            raise CtlError(f"--ssh-arg 解析失敗: {exc}")
    argv.extend(["--", host])

    if args.dry_run:
        print(format_command(argv))
        return 0

    exec_ssh(argv)
    return 0


def cmd_web(args: argparse.Namespace, cfg: Config, ctx: Ctx) -> int:
    host, cancelled = prepare_ssh_target(args, cfg, ctx)
    if cancelled:
        return 0

    port = args.port if args.port is not None else cfg.remote_web_port
    local_port = args.local_port or DEFAULT_LOCAL_PORT
    bind = normalize_web_host(args.bind, "--bind", DEFAULT_BIND)
    remote_host = normalize_web_host(
        args.remote_host, "--remote-host", DEFAULT_REMOTE_HOST
    )

    argv = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        format_forward_spec(bind, local_port, remote_host, port),
        "--",
        host,
    ]

    if not is_loopback_bind(bind):
        eprint(
            f"警告: bind={bind} 會讓其他機器連入本機 port {local_port}。"
        )

    print(format_local_url(bind, local_port))
    print("按 Ctrl-C 結束轉送")

    if args.dry_run:
        print(format_command(argv))
        return 0

    exec_ssh(argv)
    return 0


def port_type(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port 必須是整數")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port 必須介於 1 到 65535")
    return port


def add_global_args(
    parser: argparse.ArgumentParser, suppress: bool = False
) -> None:
    """Add global options to the main parser or a subparser.

    When suppress=True, defaults are argparse.SUPPRESS so parsing a subcommand
    does not overwrite global options supplied before the subcommand. This keeps
    both ``tailnetctl.py --debug status`` and ``tailnetctl.py status --debug``
    working. Do not remove this behavior when adding global args to subparsers.
    """

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    group = parser.add_argument_group("global options")
    group.add_argument(
        "--env-file",
        metavar="PATH",
        default=default(None),
        help="env 檔路徑（預設：程式目錄下的 .tailscale.env）",
    )
    group.add_argument(
        "--sudo",
        dest="sudo",
        action="store_true",
        default=default(None),
        help="以 sudo 執行 tailscale 或 systemctl",
    )
    group.add_argument(
        "--no-sudo",
        dest="sudo",
        action="store_false",
        default=default(None),
        help="不使用 sudo",
    )
    group.add_argument(
        "--no-start-daemon",
        dest="start_daemon",
        action="store_false",
        default=default(True),
        help="不要嘗試啟動 tailscaled",
    )
    group.add_argument(
        "--debug",
        action="store_true",
        default=default(False),
        help="顯示實際執行的指令（金鑰會遮蔽）",
    )


def add_join_command(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
) -> None:
    command = subparsers.add_parser(
        name,
        help=help_text,
        description="將本機加入 tailnet；已連線時預設不重新認證。",
        epilog=(
            "範例:\n"
            "  %(prog)s --dry-run\n"
            "  %(prog)s\n"
            "  %(prog)s --force"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_args(command, suppress=True)
    command.add_argument("--auth-key", metavar="KEY", help="Tailscale auth key")
    command.add_argument(
        "--auth-key-file", metavar="PATH", help="只含 auth key 的檔案"
    )
    command.add_argument(
        "--force",
        action="store_true",
        help="已加入時仍重新認證或切換 tailnet（可能中斷連線）",
    )
    command.add_argument("--reset", action="store_true", help="重設先前設定的選項")
    command.add_argument("--dry-run", action="store_true", help="只顯示指令，不執行")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="管理 Tailscale、SSH 連線與本地端口轉送。",
        epilog=(
            "指令與參數:\n"
            "  join    [--auth-key KEY] [--auth-key-file PATH] [--force]\n"
            "          [--reset] [--dry-run]\n"
            "  status  [--json]\n"
            "  list    [--json] [--online-only]\n"
            "  ssh     [PEER] [--peer PEER] [--remote-user USER]\n"
            "          [--ssh-port N] [--ssh-arg ARG] [--dry-run]\n"
            "  web     [PEER] [--peer PEER] [--port PORT] [--local-port N]\n"
            "          [--bind ADDR] [--remote-host HOST]\n"
            "          [--remote-user USER] [--dry-run]\n"
            "\n"
            "範例:\n"
            "  %(prog)s join --dry-run\n"
            "  %(prog)s status\n"
            "  %(prog)s list --online-only\n"
            "  %(prog)s ssh --peer peer-host\n"
            "  %(prog)s web --peer peer-host --port 80\n"
            "\n"
            '使用 "%(prog)s <command> --help" 查看單一指令的完整說明。'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_args(parser)

    subparsers = parser.add_subparsers(dest="command")

    add_join_command(subparsers, "join", "將本機加入 tailnet")

    status = subparsers.add_parser(
        "status",
        help="顯示本機與 tailnet 摘要",
        description=(
            "顯示 tailnet、本機狀態與 peer 數量。"
            "裝置清單請使用 list。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s\n"
            "  %(prog)s --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_args(status, suppress=True)
    status.add_argument("--json", action="store_true", help="輸出原始 JSON")

    listing = subparsers.add_parser(
        "list",
        help="列出所有裝置（含自己）",
        description=(
            "列出所有裝置（含自己），不含 tailnet／本機摘要。"
            "支援篩選與 JSON。"
        ),
        epilog=(
            "範例:\n"
            "  %(prog)s\n"
            "  %(prog)s --online-only\n"
            "  %(prog)s --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_args(listing, suppress=True)
    listing.add_argument("--json", action="store_true", help="以 JSON 輸出")
    listing.add_argument(
        "--online-only", action="store_true", help="只列出 online 的裝置"
    )

    ssh = subparsers.add_parser(
        "ssh",
        help="以 SSH 登入裝置",
        description="透過系統 ssh 登入所選 peer。",
        epilog=(
            "範例:\n"
            "  %(prog)s --peer peer-host\n"
            "  %(prog)s --peer 100.64.0.10 --remote-user root\n"
            "  %(prog)s --peer peer-host --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_args(ssh, suppress=True)
    ssh.add_argument("peer", nargs="?", help="hostname、DNSName、IP、node ID 或序號")
    ssh.add_argument(
        "--peer",
        dest="peer_opt",
        metavar="PEER",
        help="指定 peer（與位置參數 PEER 擇一）",
    )
    ssh.add_argument(
        "--remote-user",
        dest="remote_user",
        metavar="USER",
        help="遠端主機的 SSH 使用者（未指定時 TTY 詢問）",
    )
    ssh.add_argument(
        "--ssh-port", type=port_type, metavar="N", help="SSH port（預設 22）"
    )
    ssh.add_argument(
        "--ssh-arg", action="append", metavar="ARG", help="附加給 ssh 的參數（可重複）"
    )
    ssh.add_argument("--dry-run", action="store_true", help="只顯示指令，不執行")

    web = subparsers.add_parser(
        "web",
        help="建立 SSH 本地端口轉送",
        description="建立 SSH 本地端口轉送，讓遠端 port 可透過本機存取。",
        epilog=(
            "範例:\n"
            "  %(prog)s --peer peer-host --port 80\n"
            "  %(prog)s --peer peer-host --port 80 --local-port 9090\n"
            "  %(prog)s --peer peer-host --port 80 --remote-user root\n"
            "  %(prog)s --peer 100.64.0.10 --port 80 --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_global_args(web, suppress=True)
    web.add_argument("peer", nargs="?", help="hostname、DNSName、IP、node ID 或序號")
    web.add_argument(
        "--peer",
        dest="peer_opt",
        metavar="PEER",
        help="指定 peer（與位置參數 PEER 擇一）",
    )
    web.add_argument("--port", type=port_type, metavar="PORT", help="遠端 port")
    web.add_argument(
        "--local-port", type=port_type, metavar="N", help="本機 port（預設 8080）"
    )
    web.add_argument(
        "--bind", metavar="ADDR", help="本機綁定位址（預設 127.0.0.1）"
    )
    web.add_argument(
        "--remote-host", metavar="HOST", help="遠端主機（預設 127.0.0.1）"
    )
    web.add_argument(
        "--remote-user",
        dest="remote_user",
        metavar="USER",
        help="遠端主機的 SSH 使用者（未指定時 TTY 詢問）",
    )
    web.add_argument("--dry-run", action="store_true", help="只顯示指令，不執行")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        ensure_posix_supported()
        cfg = load_config(args)
        ctx = Ctx(args)
        if args.command == "join":
            return cmd_join(args, cfg, ctx)
        if args.command == "status":
            return cmd_status(args, cfg, ctx)
        if args.command == "list":
            return cmd_list(args, cfg, ctx)
        if args.command == "ssh":
            return cmd_ssh(args, cfg, ctx)
        if args.command == "web":
            return cmd_web(args, cfg, ctx)
        raise CtlError(f"未知的指令: {args.command}")
    except CtlError as exc:
        eprint(f"錯誤: {exc}")
        return 1
    except KeyboardInterrupt:
        eprint()
        eprint("已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
