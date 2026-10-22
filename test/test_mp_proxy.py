"""Tests for Factorio UDP multiplayer protocol dissector and Podman server harness."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


def find_repo_root() -> Path:
    """Find workspace repository root directory."""
    ws = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if ws and Path(ws).is_dir():
        return Path(ws)
    cur = Path(__file__).resolve()
    for parent in [cur] + list(cur.parents):
        if (parent / "MODULE.bazel").is_file():
            return parent
    return Path.cwd()


REPO_ROOT = find_repo_root()
sys.path.insert(0, str(REPO_ROOT / "foundry"))
sys.path.insert(0, str(REPO_ROOT / "flow"))

import check_blueprint
import mp_proxy

PODMAN_IMAGE = "docker.io/openroad/orfs:latest"


def _get_free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestMpProtocolUnit(unittest.TestCase):
    """Unit tests for Factorio UDP wire framing, heartbeat dissection, and injection."""

    def test_varint_roundtrip(self) -> None:
        """Verify uint16v and uint32v encoding and decoding."""
        for val in (0, 1, 42, 254, 255, 256, 0x0159, 65535):
            enc = mp_proxy.encode_uint16v(val)
            dec, pos = mp_proxy.decode_uint16v(enc, 0)
            self.assertEqual(dec, val)
            self.assertEqual(pos, len(enc))

        for val32 in (0, 127, 254, 255, 1000, 0x12345678):
            enc32 = mp_proxy.encode_uint32v(val32)
            dec32, pos32 = mp_proxy.decode_uint32v(enc32, 0)
            self.assertEqual(dec32, val32)
            self.assertEqual(pos32, len(enc32))

    def test_network_header_parsing(self) -> None:
        """Verify NetworkMessageHeader parsing for unfragmented and fragmented packets."""
        # Type 6 (ClientToServerHeartbeat), unfragmented
        raw_hb = bytes([mp_proxy.MSG_CLIENT_TO_SERVER_HEARTBEAT, 0xAA, 0xBB])
        hdr = mp_proxy.parse_network_header(raw_hb)
        self.assertEqual(hdr.msg_type, mp_proxy.MSG_CLIENT_TO_SERVER_HEARTBEAT)
        self.assertFalse(hdr.fragmented)
        self.assertIsNone(hdr.message_id)
        self.assertEqual(hdr.payload_offset, 1)

        # Type 2 (ConnectionRequest) always includes message_id
        raw_conn = bytes([mp_proxy.MSG_CONNECTION_REQUEST]) + struct.pack("<H", 0x0007) + b"payload"
        hdr_conn = mp_proxy.parse_network_header(raw_conn)
        self.assertEqual(hdr_conn.msg_type, mp_proxy.MSG_CONNECTION_REQUEST)
        self.assertEqual(hdr_conn.message_id, 7)
        self.assertEqual(hdr_conn.payload_offset, 3)

    def test_inject_and_detect_achievement_action(self) -> None:
        """Verify injecting AchievementGained (0x0159) into an empty heartbeat and detecting it."""
        # Build a ClientToServerHeartbeat with one empty TickClosure.
        # Layout: header (1B), hb_flags (1B), seq (4B), update_tick (8B), next_tick (8B).
        hb_flags = (
            mp_proxy.HB_HAS_TICK_CLOSURES
            | mp_proxy.HB_HAS_SINGLE_TICK_CLOSURE
            | mp_proxy.HB_ALL_TICK_CLOSURES_EMPTY
        )
        empty_pkt = (
            bytes([mp_proxy.MSG_CLIENT_TO_SERVER_HEARTBEAT, hb_flags])
            + struct.pack("<I", 101)
            + struct.pack("<Q", 500)
            + struct.pack("<Q", 500)
        )

        # No achievement in empty packet
        self.assertEqual(mp_proxy.scan_heartbeat_for_achievement(empty_pkt, 1), [])

        # Inject AchievementGained (0x0159) for achievement ID 1 (automate-this), player_index=0
        injected = mp_proxy.inject_action_into_empty_client_heartbeat(
            empty_pkt,
            action_type=mp_proxy.ACTION_ACHIEVEMENT_GAINED,
            player_index=0,
            action_payload=struct.pack("<H", 1),
        )
        self.assertIsNotNone(injected)
        assert injected is not None

        # Scan the modified packet and verify that the scanner detects achievement ID 1
        detected = mp_proxy.scan_heartbeat_for_achievement(injected, 1)
        self.assertEqual(detected, [1])


class TestPodmanMpProxyIntegration(unittest.TestCase):
    """Integration test for Podman MP server, displayless offscreen client, and UDP proxy."""

    def test_podman_server_proxy_and_achievement_detection(self) -> None:
        """Start a vanilla MP server in Podman, connect via UDP proxy, and detect an achievement."""
        if not shutil.which("podman"):
            self.skipTest("podman not installed")

        try:
            wrapper_bin = check_blueprint.find_factorio_binary()
        except FileNotFoundError:
            self.skipTest("Factorio binary not found")

        elf_bin = wrapper_bin.parent.parent / "x64_" / "factorio"
        if not elf_bin.is_file():
            elf_bin = wrapper_bin
        factorio_root = elf_bin.parent.parent.parent

        server_port = _get_free_udp_port()
        proxy_port = _get_free_udp_port()

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="factorilog-mp-") as tmp:
            work_dir = Path(tmp)
            server_dir = work_dir / "server"
            client_dir = work_dir / "client"
            mods_dir = work_dir / "mods"
            server_dir.mkdir(parents=True, exist_ok=True)
            client_dir.mkdir(parents=True, exist_ok=True)
            mods_dir.mkdir(parents=True, exist_ok=True)

            # Disable Space Age and expansion mods so only vanilla base is active
            mod_list = {
                "mods": [
                    {"name": "base", "enabled": True},
                    {"name": "elevated-rails", "enabled": False},
                    {"name": "quality", "enabled": False},
                    {"name": "recycler", "enabled": False},
                    {"name": "space-age", "enabled": False},
                ]
            }
            (mods_dir / "mod-list.json").write_text(json.dumps(mod_list, indent=2))

            server_cfg = server_dir / "config.ini"
            server_cfg.write_text(f"[path]\nread-data=/factorio/data\nwrite-data=/work/server\n")

            # Use 50x50 map dimensions so vanilla freeplay skips the crash-site cutscene
            map_gen = {"width": 50, "height": 50, "seed": 42}
            map_gen_file = work_dir / "map-gen-settings.json"
            map_gen_file.write_text(json.dumps(map_gen, indent=2))

            server_settings = {
                "name": "factorilog-test-server",
                "description": "Autonomous achievement test server",
                "max_players": 4,
                "visibility": {"public": False, "lan": False},
                "require_user_verification": False,
                "auto_pause": False,
            }
            settings_file = work_dir / "server-settings.json"
            settings_file.write_text(json.dumps(server_settings, indent=2))

            # Step 1: Create clean base/freeplay save inside Podman container
            cmd_create = [
                "podman",
                "run",
                "--rm",
                "--security-opt",
                "label=disable",
                "-v",
                f"{factorio_root}:/factorio:ro",
                "-v",
                f"{work_dir}:/work",
                PODMAN_IMAGE,
                "/factorio/bin/x64_/factorio",
                "-c",
                "/work/server/config.ini",
                "--mod-directory",
                "/work/mods",
                "--create",
                "/work/server/saves/mp_test.zip",
                "--map-gen-settings",
                "/work/map-gen-settings.json",
            ]
            real_home = pwd.getpwuid(os.getuid()).pw_dir
            podman_env = {
                **os.environ,
                "HOME": real_home,
                "TMPDIR": "/tmp",
            }
            res_create = subprocess.run(
                cmd_create, env=podman_env, capture_output=True, text=True, check=False
            )
            self.assertEqual(
                res_create.returncode,
                0,
                f"Map creation in Podman failed:\n{res_create.stdout}\n{res_create.stderr}",
            )
            self.assertTrue((server_dir / "saves" / "mp_test.zip").is_file())

            # Step 2: Start UDP Lockstep Proxy
            proxy = mp_proxy.FactorioLockstepProxy(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                server_host="127.0.0.1",
                server_port=server_port,
            )
            # Queue an AchievementGained (0x0159) action for achievement ID 1 (automate-this)
            # with C++ 0-based playerIndex=0 once the client enters InGame.
            proxy.queue_action(
                action_type=mp_proxy.ACTION_ACHIEVEMENT_GAINED,
                player_index=0,
                payload=struct.pack("<H", 1),
            )
            proxy.start()

            server_proc: subprocess.Popen[str] | None = None
            client_proc: subprocess.Popen[str] | None = None
            try:
                # Step 3: Launch headless MP server inside Podman with host networking
                cmd_server = [
                    "podman",
                    "run",
                    "--rm",
                    "--network",
                    "host",
                    "--security-opt",
                    "label=disable",
                    "-v",
                    f"{factorio_root}:/factorio:ro",
                    "-v",
                    f"{work_dir}:/work",
                    PODMAN_IMAGE,
                    "/factorio/bin/x64_/factorio",
                    "-c",
                    "/work/server/config.ini",
                    "--mod-directory",
                    "/work/mods",
                    "--bind",
                    f"127.0.0.1:{server_port}",
                    "--start-server",
                    "/work/server/saves/mp_test.zip",
                    "--server-settings",
                    "/work/server-settings.json",
                ]
                server_proc = subprocess.Popen(
                    cmd_server,
                    env=podman_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                # Wait for server to open UDP socket and reach InGame
                server_ready = False
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    if server_proc.poll() is not None:
                        break
                    log_path = server_dir / "factorio-current.log"
                    if log_path.is_file():
                        log_txt = log_path.read_text(errors="replace")
                        if "changing state from(CreatingGame) to(InGame)" in log_txt:
                            server_ready = True
                            break
                    time.sleep(0.1)
                if not server_ready:
                    srv_out = server_proc.stdout.read() if server_proc.stdout else ""
                    log_txt = (
                        (server_dir / "factorio-current.log").read_text(errors="replace")
                        if (server_dir / "factorio-current.log").is_file()
                        else ""
                    )
                    self.fail(
                        f"Podman MP server did not reach InGame state.\n"
                        f"STDOUT:\n{srv_out}\nLOG:\n{log_txt}"
                    )

                # Step 4: Launch displayless client (SDL_VIDEODRIVER=offscreen) connected to Proxy
                client_cfg = client_dir / "config.ini"
                client_cfg.write_text(
                    f"[path]\nread-data={factorio_root}/data\nwrite-data={client_dir}\n"
                    f"[other]\ncheck_updates=false\n"
                )
                # Copy cached player-data.json if present for username setting
                host_player_data = Path(real_home) / ".factorio" / "player-data.json"
                if host_player_data.is_file():
                    shutil.copy(host_player_data, client_dir / "player-data.json")
                else:
                    (client_dir / "player-data.json").write_text(
                        json.dumps({"service-username": "factorilog-bot"}, indent=2)
                    )

                client_env = {
                    **os.environ,
                    "HOME": real_home,
                    "SteamAppId": "427520",
                    "SteamGameId": "427520",
                    "DISPLAY": "",
                    "WAYLAND_DISPLAY": "",
                    "SDL_VIDEODRIVER": "offscreen",
                }
                cmd_client = [
                    str(elf_bin),
                    "-c",
                    str(client_cfg),
                    "--mod-directory",
                    str(mods_dir),
                    "--disable-audio",
                    "--force-graphics-preset",
                    "very-low",
                    "--video-memory-usage",
                    "low",
                    "--low-vram",
                    "--mp-connect",
                    f"127.0.0.1:{proxy_port}",
                ]
                client_proc = subprocess.Popen(
                    cmd_client,
                    env=client_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                # Wait for handshake, map download, InGame lockstep, and achievement injection/broadcast
                success = False
                join_deadline = time.monotonic() + 25.0
                while time.monotonic() < join_deadline:
                    if (
                        proxy.stats.injected_actions >= 1
                        and 1 in proxy.stats.gained_achievement_ids
                        and proxy.stats.server_to_client_packets >= 290
                    ):
                        success = True
                        break
                    time.sleep(0.1)

                client_log = ""
                if (client_dir / "factorio-current.log").is_file():
                    client_log = (client_dir / "factorio-current.log").read_text(errors="replace")
                server_log = ""
                if (server_dir / "factorio-current.log").is_file():
                    server_log = (server_dir / "factorio-current.log").read_text(errors="replace")

                self.assertGreater(
                    proxy.stats.client_to_server_packets,
                    0,
                    f"No client packets seen by proxy.\nClient log:\n{client_log}",
                )
                self.assertGreater(
                    proxy.stats.server_to_client_packets,
                    0,
                    f"No server packets seen by proxy.\nServer log:\n{server_log}",
                )
                self.assertTrue(
                    success,
                    f"Did not complete lockstep injection/detection. "
                    f"Stats: {proxy.stats}\nClient log:\n{client_log}\nServer log:\n{server_log}",
                )
            finally:
                proxy.stop()
                for proc in (client_proc, server_proc):
                    if proc is not None:
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                        if proc.stdout is not None:
                            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
