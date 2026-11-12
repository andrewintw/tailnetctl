import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tailnetctl


_TAILSCALE_ENV_NAMES = (
    "TAILSCALE_AUTH_KEY",
    "TAILSCALE_AUTH_KEY_FILE",
    "TAILSCALE_LOGIN_SERVER",
    "TAILSCALE_HOSTNAME",
    "TAILSCALE_ADVERTISE_TAGS",
    "TAILSCALE_ACCEPT_DNS",
    "TAILSCALE_REMOTE_SSH_USER",
    "TAILSCALE_REMOTE_WEB_PORT",
)
_ENV_PATCHER = None


def setUpModule():
    global _ENV_PATCHER
    _ENV_PATCHER = mock.patch.dict(
        os.environ,
        {name: "" for name in _TAILSCALE_ENV_NAMES},
        clear=False,
    )
    _ENV_PATCHER.start()


def tearDownModule():
    if _ENV_PATCHER is not None:
        _ENV_PATCHER.stop()


def self_node(online=True):
    return {
        "ID": "self-1",
        "HostName": "my-host",
        "OS": "linux",
        "Online": online,
        "UserID": "u1",
        "DNSName": "my-host.example.ts.net.",
        "TailscaleIPs": ["100.64.0.10"],
    }


def peer_node(name, online=True, ip="100.64.0.20"):
    return {
        "ID": f"id-{name}",
        "HostName": name,
        "OS": "linux",
        "Online": online,
        "UserID": "u1",
        "DNSName": f"{name}.example.ts.net.",
        "TailscaleIPs": [ip],
    }


def running_data(peers=None):
    return {
        "BackendState": "Running",
        "CurrentTailnet": {
            "Name": "example.com",
            "MagicDNSSuffix": "example.ts.net",
        },
        "User": {"u1": {"LoginName": "owner@example.com"}},
        "Self": self_node(),
        "Peer": peers or {},
    }


def needs_login_data():
    self_info = self_node(False)
    self_info["DNSName"] = ""
    self_info["TailscaleIPs"] = []
    return {
        "BackendState": "NeedsLogin",
        "CurrentTailnet": None,
        "User": {},
        "Self": self_info,
        "Peer": {},
    }


def offline_running_data():
    data = running_data({"p1": peer_node("peer-a", False)})
    data["Self"]["Online"] = False
    data["Self"]["InMagicSock"] = False
    return data


def make_args(**overrides):
    defaults = {
        "dry_run": False,
        "force": False,
        "reset": False,
        "auth_key": None,
        "auth_key_file": None,
        "sudo": False,
        "debug": False,
        "start_daemon": True,
        "env_file": None,
        "json": False,
        "online_only": False,
        "peer": None,
        "peer_opt": None,
        "remote_user": None,
        "ssh_port": None,
        "ssh_arg": None,
        "port": 8080,
        "local_port": None,
        "bind": None,
        "remote_host": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_cfg(values=None):
    return tailnetctl.Config(
        make_args(),
        Path("/nonexistent"),
        values or {},
        False,
    )


class JoinTests(unittest.TestCase):
    def test_running_without_force_prints_hint(self):
        args = make_args()
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=running_data()
        ), mock.patch.object(
            tailnetctl,
            "resolve_auth_key",
            side_effect=AssertionError("must not resolve auth key"),
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = tailnetctl.cmd_join(args, cfg, ctx)
        self.assertEqual(rc, 0)
        self.assertIn("若要重新認證或切換 tailnet", out.getvalue())

    def test_running_but_offline_join_shows_offline_hint(self):
        args = make_args()
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=offline_running_data()
        ), mock.patch.object(
            tailnetctl,
            "resolve_auth_key",
            side_effect=AssertionError("must not resolve auth key"),
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = tailnetctl.cmd_join(args, cfg, ctx)
        self.assertEqual(rc, 0)
        self.assertIn("本機目前離線", out.getvalue())
        self.assertIn("Online:       no", out.getvalue())

    def test_dry_run_running_without_force_prints_hint(self):
        args = make_args(dry_run=True)
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "try_status", return_value=(running_data(), None)
        ), mock.patch.object(
            tailnetctl,
            "resolve_auth_key",
            side_effect=AssertionError("must not resolve auth key"),
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = tailnetctl.cmd_join(args, cfg, ctx)
        self.assertEqual(rc, 0)
        self.assertIn("若要重新認證或切換 tailnet", out.getvalue())

    def test_wait_for_online_requires_self_online(self):
        ctx = tailnetctl.Ctx(make_args())
        with mock.patch.object(
            tailnetctl, "JOIN_WAIT_SECONDS", 5
        ), mock.patch.object(
            tailnetctl,
            "try_status",
            side_effect=[
                (offline_running_data(), None),
                (running_data(), None),
            ],
        ), mock.patch.object(
            tailnetctl.time, "sleep", return_value=None
        ):
            data = tailnetctl.wait_for_online(ctx, "/usr/bin/tailscale")
        self.assertTrue(tailnetctl.self_is_online(data))

    def test_dry_run_force_warns_and_redacts_key(self):
        args = make_args(dry_run=True, force=True)
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "try_status", return_value=(running_data(), None)
        ), mock.patch.object(
            tailnetctl,
            "resolve_auth_key",
            return_value=("inline", "tskey-auth-FAKEKEY"),
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = tailnetctl.cmd_join(args, cfg, ctx)
        self.assertEqual(rc, 0)
        self.assertIn("--force 會重新認證", err.getvalue())
        self.assertIn("--force-reauth", out.getvalue())
        self.assertIn("<redacted>", out.getvalue())
        self.assertNotIn("tskey-auth-FAKEKEY", out.getvalue() + err.getvalue())

    def test_join_success_cleans_temp_dir(self):
        args = make_args()
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=needs_login_data()
        ), mock.patch.object(
            tailnetctl,
            "resolve_auth_key",
            return_value=("inline", "tskey-auth-FAKEKEY"),
        ), mock.patch.object(
            tailnetctl.Ctx, "run", return_value=completed
        ) as run_mock, mock.patch.object(
            tailnetctl, "wait_for_online", return_value=running_data()
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = tailnetctl.cmd_join(args, cfg, ctx)
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        key_args = [item for item in argv if item.startswith("--auth-key=file:")]
        self.assertEqual(len(key_args), 1)
        key_path = Path(key_args[0].split("file:", 1)[1])
        self.assertFalse(key_path.parent.exists())
        self.assertIn("Tailnet:", out.getvalue())

    def test_join_failure_cleans_temp_dir(self):
        args = make_args()
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        completed = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=needs_login_data()
        ), mock.patch.object(
            tailnetctl,
            "resolve_auth_key",
            return_value=("inline", "tskey-auth-FAKEKEY"),
        ), mock.patch.object(
            tailnetctl.Ctx, "run", return_value=completed
        ) as run_mock, mock.patch.object(
            tailnetctl, "wait_for_online"
        ) as wait_mock:
            with self.assertRaises(tailnetctl.CtlError):
                tailnetctl.cmd_join(args, cfg, ctx)
        run_mock.assert_called_once()
        wait_mock.assert_not_called()
        argv = run_mock.call_args[0][0]
        key_args = [item for item in argv if item.startswith("--auth-key=file:")]
        self.assertEqual(len(key_args), 1)
        key_path = Path(key_args[0].split("file:", 1)[1])
        self.assertFalse(key_path.parent.exists())

    def test_login_is_not_a_command(self):
        parser = tailnetctl.build_parser()
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            parser.parse_args(["login"])
        self.assertIn("invalid choice", err.getvalue())


class AuthKeyTests(unittest.TestCase):
    def test_cli_key_beats_env_file_key_file(self):
        cfg = tailnetctl.Config(
            None,
            Path("/nonexistent"),
            {"TAILSCALE_AUTH_KEY_FILE": "/nonexistent/key"},
            False,
        )
        args = make_args(auth_key="tskey-auth-FAKEKEY")
        key = tailnetctl.resolve_auth_key(args, cfg)
        self.assertEqual(key, ("inline", "tskey-auth-FAKEKEY"))

    def test_cli_key_file_beats_cli_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "key"
            key_path.write_text("tskey-auth-FILEKEY\n", encoding="utf-8")
            cfg = tailnetctl.Config(None, Path("/nonexistent"), {}, False)
            args = make_args(
                auth_key="tskey-auth-INLINE", auth_key_file=str(key_path)
            )
            key = tailnetctl.resolve_auth_key(args, cfg)
            self.assertEqual(key, ("file", str(key_path)))

    def test_shell_key_beats_env_file_key_file(self):
        cfg = tailnetctl.Config(
            None,
            Path("/nonexistent"),
            {"TAILSCALE_AUTH_KEY_FILE": "/nonexistent/key"},
            False,
        )
        args = make_args()
        with mock.patch.dict(
            os.environ,
            {
                "TAILSCALE_AUTH_KEY": "tskey-auth-SHELLKEY",
                "TAILSCALE_AUTH_KEY_FILE": "",
            },
            clear=False,
        ):
            key = tailnetctl.resolve_auth_key(args, cfg)
        self.assertEqual(key, ("inline", "tskey-auth-SHELLKEY"))

    def test_key_value_in_key_file_variable_is_rejected_safely(self):
        cfg = tailnetctl.Config(
            None,
            Path("/nonexistent"),
            {"TAILSCALE_AUTH_KEY_FILE": "tskey-auth-FAKEKEY"},
            False,
        )
        args = make_args()
        with self.assertRaises(tailnetctl.CtlError) as raised:
            tailnetctl.resolve_auth_key(args, cfg)
        message = str(raised.exception)
        self.assertIn("必須是檔案路徑", message)
        self.assertNotIn("tskey-auth-FAKEKEY", message)

    def test_env_file_key_file_beats_env_file_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "key"
            key_path.write_text("tskey-auth-FILEKEY\n", encoding="utf-8")
            cfg = tailnetctl.Config(
                None,
                Path("/nonexistent"),
                {
                    "TAILSCALE_AUTH_KEY_FILE": str(key_path),
                    "TAILSCALE_AUTH_KEY": "tskey-auth-INLINE",
                },
                False,
            )
            args = make_args()
            key = tailnetctl.resolve_auth_key(args, cfg)
            self.assertEqual(key, ("file", str(key_path)))


class ConfigTests(unittest.TestCase):
    def test_remote_web_port_uses_valid_value(self):
        cfg = make_cfg({"TAILSCALE_REMOTE_WEB_PORT": "8081"})
        self.assertEqual(cfg.remote_web_port, 8081)

    def test_remote_web_port_falls_back_for_invalid_value(self):
        for value in ("abc", "0", "70000"):
            with self.subTest(value=value):
                cfg = make_cfg({"TAILSCALE_REMOTE_WEB_PORT": value})
                self.assertEqual(cfg.remote_web_port, 80)

    def test_get_prefers_shell_over_env_file(self):
        cfg = make_cfg({"TAILSCALE_REMOTE_SSH_USER": "file-user"})
        with mock.patch.dict(
            os.environ,
            {"TAILSCALE_REMOTE_SSH_USER": "shell-user"},
            clear=False,
        ):
            self.assertEqual(
                cfg.get("TAILSCALE_REMOTE_SSH_USER"), "shell-user"
            )
        self.assertEqual(cfg.get("TAILSCALE_REMOTE_SSH_USER"), "file-user")


class MaterializeKeyTests(unittest.TestCase):
    def test_inline_key_uses_private_temp_dir(self):
        key_arg, temp_dir = tailnetctl.materialize_key(
            ("inline", "tskey-auth-FAKEKEY")
        )
        try:
            self.assertIsNotNone(temp_dir)
            key_path = Path(key_arg.split("file:", 1)[1])
            self.assertTrue(key_path.is_file())
            self.assertEqual(
                key_path.read_text(encoding="utf-8").strip(),
                "tskey-auth-FAKEKEY",
            )
            self.assertEqual(stat.S_IMODE(temp_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def test_file_key_is_copied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "key"
            source.write_text("tskey-auth-FILEKEY\n", encoding="utf-8")
            key_arg, temp_dir = tailnetctl.materialize_key(
                ("file", str(source))
            )
            try:
                key_path = Path(key_arg.split("file:", 1)[1])
                self.assertNotEqual(str(key_path), str(source))
                self.assertEqual(
                    key_path.read_text(encoding="utf-8").strip(),
                    "tskey-auth-FILEKEY",
                )
            finally:
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)


class EnvFileTests(unittest.TestCase):
    def parse(self, content):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "env"
            path.write_text(content, encoding="utf-8")
            return tailnetctl.parse_env_file(path)

    def test_inline_comments_are_removed(self):
        values = self.parse(
            "TAILSCALE_HOSTNAME=my-node # test host\n"
            "TAILSCALE_ADVERTISE_TAGS=tag:dev,tag:linux # tags\n"
        )
        self.assertEqual(values["TAILSCALE_HOSTNAME"], "my-node")
        self.assertEqual(
            values["TAILSCALE_ADVERTISE_TAGS"], "tag:dev,tag:linux"
        )

    def test_hash_without_whitespace_is_kept(self):
        values = self.parse(
            "URL=https://example.com/page#section\n"
        )
        self.assertEqual(
            values["URL"], "https://example.com/page#section"
        )

    def test_quoted_hash_is_kept(self):
        values = self.parse(
            'A="value # kept"\n'
            "B='value # kept' # outer comment\n"
        )
        self.assertEqual(values["A"], "value # kept")
        self.assertEqual(values["B"], "value # kept")

    def test_double_quoted_escaped_quote(self):
        values = self.parse('A="my\\"node"\n')
        self.assertEqual(values["A"], 'my"node')

    def test_double_quoted_escaped_backslash(self):
        values = self.parse('A="C:\\\\path"\n')
        self.assertEqual(values["A"], "C:\\path")

    def test_single_quoted_backslash_is_literal(self):
        values = self.parse("A='my\\\"node'\n")
        self.assertEqual(values["A"], 'my\\"node')

    def test_unquoted_backslash_is_literal(self):
        values = self.parse("A=value\\path\n")
        self.assertEqual(values["A"], "value\\path")

    def test_other_double_quote_escapes_are_preserved(self):
        values = self.parse('A="line\\n"\n')
        self.assertEqual(values["A"], "line\\n")

    def test_export_and_blank_lines(self):
        values = self.parse("\n# comment\nexport A=value # comment\n")
        self.assertEqual(values["A"], "value")


class PlatformTests(unittest.TestCase):
    def test_posix_is_allowed(self):
        tailnetctl.ensure_posix_supported("posix")

    def test_non_posix_is_rejected(self):
        with self.assertRaises(tailnetctl.CtlError) as raised:
            tailnetctl.ensure_posix_supported("nt")
        self.assertIn("WSL", str(raised.exception))


class GlobalArgsTests(unittest.TestCase):
    def test_global_flags_before_and_after_subcommand(self):
        parser = tailnetctl.build_parser()
        for argv in (["--debug", "status"], ["status", "--debug"]):
            with self.subTest(argv=argv):
                self.assertTrue(parser.parse_args(argv).debug)
        for argv in (["--sudo", "status"], ["status", "--sudo"]):
            with self.subTest(argv=argv):
                self.assertTrue(parser.parse_args(argv).sudo)
        for argv in (["--no-sudo", "status"], ["status", "--no-sudo"]):
            with self.subTest(argv=argv):
                self.assertFalse(parser.parse_args(argv).sudo)
        for argv in (
            ["--no-start-daemon", "status"],
            ["status", "--no-start-daemon"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(parser.parse_args(argv).start_daemon)

    def test_subparser_does_not_overwrite_defaults(self):
        args = tailnetctl.build_parser().parse_args(["status"])
        self.assertFalse(args.debug)
        self.assertIsNone(args.sudo)
        self.assertTrue(args.start_daemon)


class PrepareSshTargetTests(unittest.TestCase):
    def run_prepare(self, peer, user_result):
        args = make_args(peer_opt="peer-a", dry_run=True)
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        data = running_data({"p1": peer_node("peer-a", True)})
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=data
        ), mock.patch.object(
            tailnetctl, "select_peer", return_value=peer
        ), mock.patch.object(
            tailnetctl, "resolve_remote_ssh_user", return_value=user_result
        ):
            return tailnetctl.prepare_ssh_target(args, cfg, ctx)

    def test_builds_user_at_host(self):
        host, cancelled = self.run_prepare(
            peer_node("peer-a", True), ("alice", False)
        )
        self.assertEqual(host, "alice@peer-a.example.ts.net")
        self.assertFalse(cancelled)

    def test_builds_host_without_user(self):
        host, cancelled = self.run_prepare(
            peer_node("peer-a", True), (None, False)
        )
        self.assertEqual(host, "peer-a.example.ts.net")
        self.assertFalse(cancelled)

    def test_cancelled_when_peer_selection_is_cancelled(self):
        host, cancelled = self.run_prepare(None, (None, False))
        self.assertIsNone(host)
        self.assertTrue(cancelled)

    def test_cancelled_when_user_selection_is_cancelled(self):
        host, cancelled = self.run_prepare(
            peer_node("peer-a", True), (None, True)
        )
        self.assertIsNone(host)
        self.assertTrue(cancelled)

    def test_target_starting_with_dash_is_rejected(self):
        args = make_args(peer_opt="peer-a", dry_run=True)
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        data = running_data({"p1": peer_node("peer-a", True)})
        peer = peer_node("peer-a", True)
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=data
        ), mock.patch.object(
            tailnetctl, "select_peer", return_value=peer
        ), mock.patch.object(
            tailnetctl, "peer_target", return_value="-evil"
        ):
            with self.assertRaises(tailnetctl.CtlError) as raised:
                tailnetctl.prepare_ssh_target(args, cfg, ctx)
        self.assertIn("peer 位址", str(raised.exception))


class StatusTests(unittest.TestCase):
    def test_needs_login_prints_hint(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tailnetctl.print_status(needs_login_data())
        text = out.getvalue()
        self.assertIn("尚未登入 tailnet", text)
        self.assertIn("join", text)

    def test_running_but_offline_shows_cache_hint(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tailnetctl.print_status(offline_running_data())
        text = out.getvalue()
        self.assertIn("Online:       no", text)
        self.assertIn("快取狀態", text)


class ListTests(unittest.TestCase):
    def run_list(self, args, data):
        cfg = make_cfg()
        ctx = tailnetctl.Ctx(args)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=data
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = tailnetctl.cmd_list(args, cfg, ctx)
        return rc, out.getvalue(), err.getvalue()

    def test_json_includes_self_first(self):
        peers = {
            "p1": peer_node("peer-a", True),
            "p2": peer_node("peer-b", False, "100.64.0.30"),
        }
        rc, out, err = self.run_list(make_args(json=True), running_data(peers))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertTrue(payload[0]["self"])
        self.assertEqual(payload[0]["hostname"], "my-host")
        self.assertFalse(payload[1]["self"])

    def test_online_only_keeps_online_self(self):
        peers = {
            "p1": peer_node("peer-a", False),
            "p2": peer_node("peer-b", True, "100.64.0.30"),
        }
        rc, out, err = self.run_list(
            make_args(json=True, online_only=True), running_data(peers)
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        names = [item["hostname"] for item in payload]
        self.assertEqual(names, ["my-host", "peer-b"])

    def test_running_but_offline_warns_and_marks_self_offline(self):
        rc, out, err = self.run_list(
            make_args(json=True), offline_running_data()
        )
        self.assertEqual(rc, 0)
        self.assertIn("本機目前離線", err)
        payload = json.loads(out)
        self.assertTrue(payload[0]["self"])
        self.assertFalse(payload[0]["online"])

    def test_online_only_excludes_offline_self(self):
        rc, out, err = self.run_list(
            make_args(json=True, online_only=True), offline_running_data()
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])

    def test_needs_login_warns(self):
        rc, out, err = self.run_list(make_args(json=True), needs_login_data())
        self.assertEqual(rc, 0)
        self.assertIn("警告", err)
        self.assertIn("NeedsLogin", err)
        payload = json.loads(out)
        self.assertTrue(payload[0]["self"])
        self.assertFalse(payload[0]["online"])


class RemoteUserTests(unittest.TestCase):
    def test_cli_remote_user_wins(self):
        args = make_args(remote_user="alice")
        cfg = make_cfg({"TAILSCALE_REMOTE_SSH_USER": "bob"})
        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            args, cfg, is_tty=True, prompt=lambda _: "carol"
        )
        self.assertEqual((user, cancelled), ("alice", False))

    def test_env_remote_user_wins(self):
        args = make_args()
        cfg = make_cfg({"TAILSCALE_REMOTE_SSH_USER": "bob"})
        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            args, cfg, is_tty=True, prompt=lambda _: "carol"
        )
        self.assertEqual((user, cancelled), ("bob", False))

    def test_prompt_returns_entered_user(self):
        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            make_args(), make_cfg(), is_tty=True, prompt=lambda _: "carol"
        )
        self.assertEqual((user, cancelled), ("carol", False))

    def test_cli_remote_user_starting_with_dash_is_rejected(self):
        args = make_args(remote_user="-oProxyCommand=evil")
        with self.assertRaises(tailnetctl.CtlError):
            tailnetctl.resolve_remote_ssh_user(args, make_cfg())

    def test_env_remote_user_starting_with_dash_is_rejected(self):
        cfg = make_cfg({"TAILSCALE_REMOTE_SSH_USER": "-oProxyCommand=evil"})
        with self.assertRaises(tailnetctl.CtlError):
            tailnetctl.resolve_remote_ssh_user(make_args(), cfg)

    def test_prompted_remote_user_starting_with_dash_is_rejected(self):
        with self.assertRaises(tailnetctl.CtlError):
            tailnetctl.resolve_remote_ssh_user(
                make_args(),
                make_cfg(),
                is_tty=True,
                prompt=lambda _: "-oProxyCommand=evil",
            )

    def test_enter_uses_ssh_default(self):
        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            make_args(), make_cfg(), is_tty=True, prompt=lambda _: ""
        )
        self.assertEqual((user, cancelled), (None, False))

    def test_q_cancels_prompt(self):
        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            make_args(), make_cfg(), is_tty=True, prompt=lambda _: "q"
        )
        self.assertEqual((user, cancelled), (None, True))

    def test_eof_cancels_prompt(self):
        def raise_eof(_):
            raise EOFError

        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            make_args(), make_cfg(), is_tty=True, prompt=raise_eof
        )
        self.assertEqual((user, cancelled), (None, True))

    def test_non_tty_does_not_prompt(self):
        prompt = mock.Mock()
        user, cancelled = tailnetctl.resolve_remote_ssh_user(
            make_args(), make_cfg(), is_tty=False, prompt=prompt
        )
        self.assertEqual((user, cancelled), (None, False))
        prompt.assert_not_called()

    def test_old_user_option_removed(self):
        parser = tailnetctl.build_parser()
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            parser.parse_args(["ssh", "--user", "root"])
        self.assertIn("unrecognized arguments", err.getvalue())


class PeerQueryTests(unittest.TestCase):
    def test_positional_only(self):
        args = make_args(peer="host1")
        self.assertEqual(tailnetctl.resolve_peer_query(args), "host1")

    def test_option_only(self):
        args = make_args(peer_opt="host2")
        self.assertEqual(tailnetctl.resolve_peer_query(args), "host2")

    def test_same_value(self):
        args = make_args(peer="host1", peer_opt="host1")
        self.assertEqual(tailnetctl.resolve_peer_query(args), "host1")

    def test_conflicting_values_raise(self):
        args = make_args(peer="host1", peer_opt="host2")
        with self.assertRaises(tailnetctl.CtlError) as raised:
            tailnetctl.resolve_peer_query(args)
        self.assertIn("請只保留一個", str(raised.exception))

    def test_empty_positional_peer_raises(self):
        with self.assertRaises(tailnetctl.CtlError) as raised:
            tailnetctl.resolve_peer_query(make_args(peer=""))
        self.assertIn("不可為空白", str(raised.exception))

    def test_empty_option_peer_raises(self):
        with self.assertRaises(tailnetctl.CtlError) as raised:
            tailnetctl.resolve_peer_query(make_args(peer_opt=""))
        self.assertIn("不可為空白", str(raised.exception))

    def test_whitespace_option_peer_raises(self):
        with self.assertRaises(tailnetctl.CtlError) as raised:
            tailnetctl.resolve_peer_query(make_args(peer_opt="   "))
        self.assertIn("不可為空白", str(raised.exception))


class PeerSelectionTests(unittest.TestCase):
    def test_non_tty_error_mentions_peer_and_option(self):
        data = running_data({"p1": peer_node("peer-a", True)})
        fake = SimpleNamespace(isatty=lambda: False)
        with mock.patch.object(
            tailnetctl.sys, "stdin", fake
        ), mock.patch.object(tailnetctl.sys, "stdout", fake):
            with self.assertRaises(tailnetctl.CtlError) as raised:
                tailnetctl.select_peer(data, None)
        message = str(raised.exception)
        self.assertIn("PEER", message)
        self.assertIn("--peer", message)


class WebFormatTests(unittest.TestCase):
    def test_default_local_url(self):
        self.assertEqual(
            tailnetctl.format_local_url("127.0.0.1", 8080),
            "http://127.0.0.1:8080/",
        )

    def test_ipv4_wildcard_local_url(self):
        self.assertEqual(
            tailnetctl.format_local_url("0.0.0.0", 8080),
            "http://127.0.0.1:8080/",
        )

    def test_ipv6_loopback_local_url(self):
        self.assertEqual(
            tailnetctl.format_local_url("::1", 8080),
            "http://[::1]:8080/",
        )

    def test_ipv6_wildcard_local_url(self):
        self.assertEqual(
            tailnetctl.format_local_url("::", 8080),
            "http://[::1]:8080/",
        )

    def test_custom_bind_local_url(self):
        self.assertEqual(
            tailnetctl.format_local_url("192.0.2.10", 8080),
            "http://192.0.2.10:8080/",
        )

    def test_forward_spec_ipv4(self):
        self.assertEqual(
            tailnetctl.format_forward_spec(
                "127.0.0.1", 8080, "127.0.0.1", 80
            ),
            "127.0.0.1:8080:127.0.0.1:80",
        )

    def test_forward_spec_ipv6(self):
        self.assertEqual(
            tailnetctl.format_forward_spec(
                "::1", 8080, "2001:db8::1", 80
            ),
            "[::1]:8080:[2001:db8::1]:80",
        )

    def test_bracketed_loopback(self):
        self.assertTrue(tailnetctl.is_loopback_bind("[::1]"))
        self.assertFalse(tailnetctl.is_loopback_bind("0.0.0.0"))

    def test_normalize_web_host_defaults(self):
        self.assertEqual(
            tailnetctl.normalize_web_host(None, "--bind", "127.0.0.1"),
            "127.0.0.1",
        )

    def test_normalize_web_host_trims(self):
        self.assertEqual(
            tailnetctl.normalize_web_host(
                " 10.0.0.5 ", "--remote-host", "127.0.0.1"
            ),
            "10.0.0.5",
        )

    def test_normalize_web_host_rejects_blank(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaises(tailnetctl.CtlError) as raised:
                    tailnetctl.normalize_web_host(
                        value, "--bind", "127.0.0.1"
                    )
                self.assertIn("不可為空白", str(raised.exception))


class SshWebTests(unittest.TestCase):
    def run_command(self, func, args, data, cfg=None):
        cfg = cfg or make_cfg()
        ctx = tailnetctl.Ctx(args)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            tailnetctl, "require_tailscale", return_value="/usr/bin/tailscale"
        ), mock.patch.object(
            tailnetctl, "get_status", return_value=data
        ), redirect_stdout(
            out
        ), redirect_stderr(
            err
        ):
            rc = func(args, cfg, ctx)
        return rc, out.getvalue(), err.getvalue()

    def test_ssh_blocked_when_needs_login(self):
        with self.assertRaises(tailnetctl.CtlError) as raised:
            self.run_command(
                tailnetctl.cmd_ssh, make_args(peer="1"), needs_login_data()
            )
        self.assertIn("join", str(raised.exception))

    def test_web_blocked_when_needs_login(self):
        with self.assertRaises(tailnetctl.CtlError) as raised:
            self.run_command(
                tailnetctl.cmd_web,
                make_args(peer="1", port=80),
                needs_login_data(),
            )
        self.assertIn("join", str(raised.exception))

    def test_ssh_and_web_blocked_when_self_offline(self):
        cases = (
            (tailnetctl.cmd_ssh, make_args(peer="1")),
            (tailnetctl.cmd_web, make_args(peer="1", port=80)),
        )
        for func, args in cases:
            with self.subTest(func=func.__name__):
                with self.assertRaises(tailnetctl.CtlError) as raised:
                    self.run_command(func, args, offline_running_data())
                self.assertIn("離線", str(raised.exception))

    def test_ssh_builds_expected_command(self):
        peers = {"p1": peer_node("peer-a", True)}
        args = make_args(
            peer_opt="peer-a",
            remote_user="alice",
            ssh_port=2222,
            ssh_arg=["-v"],
            dry_run=True,
        )
        rc, out, err = self.run_command(
            tailnetctl.cmd_ssh, args, running_data(peers)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn(
            "ssh -p 2222 -v -- alice@peer-a.example.ts.net", out
        )

    def test_ssh_uses_prompted_remote_user(self):
        peers = {"p1": peer_node("peer-a", True)}
        args = make_args(peer_opt="peer-a", dry_run=True)
        with mock.patch.object(
            tailnetctl,
            "resolve_remote_ssh_user",
            return_value=("ubuntu", False),
        ):
            rc, out, err = self.run_command(
                tailnetctl.cmd_ssh, args, running_data(peers)
            )
        self.assertEqual(rc, 0)
        self.assertIn("ssh -- ubuntu@peer-a.example.ts.net", out)

    def test_web_uses_remote_web_port_env(self):
        peers = {"p1": peer_node("peer-a", True)}
        args = make_args(peer_opt="peer-a", port=None, dry_run=True)
        cfg = make_cfg({"TAILSCALE_REMOTE_WEB_PORT": "8081"})
        rc, out, err = self.run_command(
            tailnetctl.cmd_web, args, running_data(peers), cfg
        )
        self.assertEqual(rc, 0)
        self.assertIn("-L 127.0.0.1:8080:127.0.0.1:8081", out)

    def test_web_builds_expected_command(self):
        peers = {"p1": peer_node("peer-a", True)}
        args = make_args(
            peer_opt="peer-a",
            remote_user="alice",
            port=8081,
            local_port=9090,
            bind="0.0.0.0",
            remote_host="10.0.0.5",
            dry_run=True,
        )
        rc, out, err = self.run_command(
            tailnetctl.cmd_web, args, running_data(peers)
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "-L 0.0.0.0:9090:10.0.0.5:8081", out
        )
        self.assertIn("-- alice@peer-a.example.ts.net", out)
        self.assertIn("警告", err)

    def test_web_ipv6_bind_dry_run(self):
        peers = {"p1": peer_node("peer-a", True)}
        args = make_args(
            peer_opt="peer-a",
            remote_user="alice",
            port=80,
            bind="::1",
            dry_run=True,
        )
        rc, out, err = self.run_command(
            tailnetctl.cmd_web, args, running_data(peers)
        )
        self.assertEqual(rc, 0)
        self.assertIn("http://[::1]:8080/", out)
        self.assertIn("[::1]:8080:127.0.0.1:80", out)
        self.assertIn("-- alice@peer-a.example.ts.net", out)
        self.assertEqual(err, "")

    def test_conflicting_peer_arguments_are_rejected(self):
        peers = {"p1": peer_node("peer-a", True)}
        args = make_args(peer="peer-a", peer_opt="peer-b", dry_run=True)
        with self.assertRaises(tailnetctl.CtlError):
            self.run_command(
                tailnetctl.cmd_ssh, args, running_data(peers)
            )

    def test_web_rejects_blank_bind_or_remote_host(self):
        peers = {"p1": peer_node("peer-a", True)}
        cases = (
            make_args(peer_opt="peer-a", port=80, bind="   ", dry_run=True),
            make_args(
                peer_opt="peer-a",
                port=80,
                remote_host="   ",
                dry_run=True,
            ),
        )
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(tailnetctl.CtlError):
                    self.run_command(
                        tailnetctl.cmd_web, args, running_data(peers)
                    )


class DaemonTests(unittest.TestCase):
    def test_get_status_starts_daemon_and_retries(self):
        ctx = tailnetctl.Ctx(make_args())
        with mock.patch.object(
            tailnetctl,
            "try_status",
            side_effect=[
                (None, "connection refused"),
                (running_data(), None),
            ],
        ) as status_mock, mock.patch.object(
            tailnetctl, "try_start_daemon", return_value=True
        ) as start_mock, mock.patch.object(
            tailnetctl.time, "sleep", return_value=None
        ):
            data = tailnetctl.get_status(ctx, "/usr/bin/tailscale")
        self.assertEqual(data["BackendState"], "Running")
        start_mock.assert_called_once()
        self.assertEqual(status_mock.call_count, 2)

    def test_get_status_permission_denied_does_not_start(self):
        ctx = tailnetctl.Ctx(make_args())
        with mock.patch.object(
            tailnetctl, "try_status", return_value=(None, "permission denied")
        ), mock.patch.object(
            tailnetctl, "try_start_daemon"
        ) as start_mock:
            with self.assertRaises(tailnetctl.CtlError) as raised:
                tailnetctl.get_status(ctx, "/usr/bin/tailscale")
        start_mock.assert_not_called()
        self.assertIn("permission denied", str(raised.exception))

    def test_try_start_daemon_uses_systemctl(self):
        ctx = tailnetctl.Ctx(make_args())
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")

        def which(name):
            return "/bin/systemctl" if name == "systemctl" else None

        with mock.patch.object(
            tailnetctl.shutil, "which", side_effect=which
        ), mock.patch.object(
            tailnetctl.Path, "is_dir", return_value=True
        ), mock.patch.object(
            tailnetctl.Ctx, "run", return_value=completed
        ) as run_mock, redirect_stderr(
            io.StringIO()
        ):
            result = tailnetctl.try_start_daemon(ctx)
        self.assertTrue(result)
        self.assertEqual(
            run_mock.call_args[0][0],
            ["/bin/systemctl", "start", "tailscaled"],
        )


class EnvPermissionTests(unittest.TestCase):
    def test_load_config_warns_for_0644(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".tailscale.env"
            path.write_text("", encoding="utf-8")
            os.chmod(path, 0o644)
            err = io.StringIO()
            with redirect_stderr(err):
                tailnetctl.load_config(make_args(env_file=str(path)))
        self.assertIn("chmod 600", err.getvalue())

    def test_load_config_is_silent_for_0600(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".tailscale.env"
            path.write_text("", encoding="utf-8")
            os.chmod(path, 0o600)
            err = io.StringIO()
            with redirect_stderr(err):
                tailnetctl.load_config(make_args(env_file=str(path)))
        self.assertEqual(err.getvalue(), "")


class InstallHintTests(unittest.TestCase):
    def test_linux_hint(self):
        message = tailnetctl.missing_tailscale_message("tailscale", "linux")
        self.assertIn(
            "curl -fsSL https://tailscale.com/install.sh | sh", message
        )
        self.assertNotIn("https://tailscale.com/download", message)

    def test_non_linux_hint(self):
        message = tailnetctl.missing_tailscale_message("tailscale", "darwin")
        self.assertIn("https://tailscale.com/download", message)
        self.assertNotIn("install.sh", message)

    def test_require_tailscale_uses_linux_hint(self):
        with mock.patch.object(
            tailnetctl.shutil, "which", return_value=None
        ), mock.patch.object(tailnetctl.sys, "platform", "linux"):
            with self.assertRaises(tailnetctl.CtlError) as raised:
                tailnetctl.require_tailscale()
        self.assertIn("curl -fsSL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
