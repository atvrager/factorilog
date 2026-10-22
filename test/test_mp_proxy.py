"""Tests for Factorio UDP multiplayer protocol dissector and Podman server harness."""

from __future__ import annotations

import json
import math
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

    def test_physical_action_builders(self) -> None:
        """Verify binary encoding of physical character InputAction builders."""
        act_stop_w, pay_stop_w = mp_proxy.build_stop_walking_action()
        self.assertEqual(act_stop_w, mp_proxy.ACTION_STOP_WALKING)
        self.assertEqual(pay_stop_w, b"")

        act_walk, pay_walk = mp_proxy.build_start_walking_action(1.0, -0.5)
        self.assertEqual(act_walk, mp_proxy.ACTION_START_WALKING)
        self.assertEqual(len(pay_walk), 16)
        self.assertEqual(struct.unpack("<dd", pay_walk), (1.0, -0.5))

        act_sel, pay_sel = mp_proxy.build_selected_entity_action(2.5, -1.25)
        self.assertEqual(act_sel, mp_proxy.ACTION_SELECTED_ENTITY_CHANGED)
        self.assertEqual(struct.unpack("<ii", pay_sel), (640, -320))

        act_craft, pay_craft = mp_proxy.build_craft_action(42, 3)
        self.assertEqual(act_craft, mp_proxy.ACTION_CRAFT)
        self.assertEqual(struct.unpack("<HI", pay_craft), (42, 3))

        act_pip, pay_pip = mp_proxy.build_pipette_item_action(99)
        self.assertEqual(act_pip, mp_proxy.ACTION_PIPETTE)
        self.assertEqual(len(pay_pip), 10)
        self.assertEqual(struct.unpack("<BHHHBBB", pay_pip), (0, 99, 0, 0, 0, 0, 0))

        act_bld, pay_bld = mp_proxy.build_place_entity_action(3.0, 4.0, direction=2)
        self.assertEqual(act_bld, mp_proxy.ACTION_BUILD)
        self.assertEqual(len(pay_bld), 12)
        self.assertEqual(struct.unpack("<iiBBBB", pay_bld), (768, 1024, 2, 0, 0, 0))

    def test_strip_client_cancellation_actions(self) -> None:
        """Verify stripping client-idle StopWalking/StopMining while preserving non-suppressed actions."""
        hb_flags = (
            mp_proxy.HB_HAS_TICK_CLOSURES
            | mp_proxy.HB_HAS_SINGLE_TICK_CLOSURE
            | mp_proxy.HB_ALL_TICK_CLOSURES_EMPTY
        )
        empty_pkt = (
            bytes([mp_proxy.MSG_CLIENT_TO_SERVER_HEARTBEAT, hb_flags])
            + struct.pack("<I", 10)
            + struct.pack("<Q", 200)
            + struct.pack("<Q", 200)
        )
        # Inject StopMining (0x0003) into the empty packet
        stop_mine_pkt = mp_proxy.inject_action_into_empty_client_heartbeat(
            empty_pkt,
            action_type=mp_proxy.ACTION_STOP_MINING,
            player_index=0,
            action_payload=b"",
        )
        assert stop_mine_pkt is not None
        self.assertEqual(
            stop_mine_pkt[1] & mp_proxy.HB_ALL_TICK_CLOSURES_EMPTY,
            0,
        )

        # Stripping cancellation actions restores the empty heartbeat byte-for-byte
        stripped = mp_proxy.strip_client_cancellation_actions(stop_mine_pkt)
        self.assertEqual(stripped, empty_pkt)

        # Build a 2-action closure containing StopMining (0x0003) + AchievementGained (0x0159)
        header = (
            bytes(
                [
                    mp_proxy.MSG_CLIENT_TO_SERVER_HEARTBEAT,
                    hb_flags & ~mp_proxy.HB_ALL_TICK_CLOSURES_EMPTY,
                ]
            )
            + struct.pack("<I", 11)
            + struct.pack("<Q", 201)
        )
        count_2 = mp_proxy.encode_uint32v(2 << 1)
        # First action: StopMining for player 0 (delta = 1 from 0xFFFF)
        act1 = mp_proxy.encode_uint16v(mp_proxy.ACTION_STOP_MINING) + mp_proxy.encode_uint16v(1)
        # Second action: AchievementGained(7) for player 0 (delta = 0 from player 0)
        act2 = (
            mp_proxy.encode_uint16v(mp_proxy.ACTION_ACHIEVEMENT_GAINED)
            + mp_proxy.encode_uint16v(0)
            + struct.pack("<H", 7)
        )
        mixed_pkt = header + count_2 + act1 + act2 + struct.pack("<Q", 201)
        stripped_mixed = mp_proxy.strip_client_cancellation_actions(mixed_pkt)
        # AchievementGained(7) must remain intact with updated player delta=1
        self.assertEqual(mp_proxy.scan_heartbeat_for_achievement(stripped_mixed, 1), [7])

    def test_compute_walking_vector(self) -> None:
        """Verify normalized 2D walking vector calculation."""
        dx, dy = mp_proxy.compute_walking_vector(0.0, 0.0, 3.0, 4.0)
        self.assertAlmostEqual(dx, 0.6)
        self.assertAlmostEqual(dy, 0.8)
        self.assertEqual(mp_proxy.compute_walking_vector(5.0, 5.0, 5.0, 5.0), (0.0, 0.0))


class TestPodmanMpProxyIntegration(unittest.TestCase):
    """Integration test for Podman MP server, displayless offscreen client, and UDP proxy."""

    def test_podman_server_proxy_and_achievement_detection(self) -> None:
        """Start a vanilla MP server in Podman, play out an achievement via physical InputActions, and detect it."""
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
            client_home = work_dir / "client_home"
            mods_dir = work_dir / "mods"
            server_dir.mkdir(parents=True, exist_ok=True)
            client_dir.mkdir(parents=True, exist_ok=True)
            client_home.mkdir(parents=True, exist_ok=True)
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
            server_cfg.write_text("[path]\nread-data=/factorio/data\nwrite-data=/work/server\n")

            # Use 256x256 map dimensions so natural starting mineral patches spawn
            map_gen = {"width": 256, "height": 256, "seed": 42}
            map_gen_file = work_dir / "map-gen-settings.json"
            map_gen_file.write_text(json.dumps(map_gen, indent=2))

            # Enable auto_pause so map->ticksPlayed stays 0 until the client joins,
            # satisfying player->onlineTime > map->ticksPlayed / 2 in AchievementStats::allowed.
            server_settings = {
                "name": "factorilog-test-server",
                "description": "Autonomous achievement test server",
                "max_players": 4,
                "visibility": {"public": False, "lan": False},
                "require_user_verification": False,
                "auto_pause": True,
            }
            settings_file = work_dir / "server-settings.json"
            settings_file.write_text(json.dumps(server_settings, indent=2))

            # Step 1: Create clean base/freeplay save inside Podman container
            save_zip = server_dir / "saves" / "mp_test.zip"
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
            self.assertTrue(save_zip.is_file())

            # Step 2: Dynamically extract runtime prototype IDs from level.dat0 in mp_test.zip
            id_mappings = mp_proxy.extract_save_id_mappings(save_zip)
            build_ach_map = id_mappings.get("build-entity-achievement", {})
            item_map = id_mappings.get("item", {})
            self.assertTrue(
                build_ach_map,
                f"Failed to extract build-entity-achievement mappings from save: {list(id_mappings)}",
            )
            self.assertTrue(
                item_map,
                f"Failed to extract item mappings from save: {list(id_mappings)}",
            )

            # Dynamically resolve the target achievement ID and placeable entity item ID
            target_ach_name = next(
                (name for name in build_ach_map if "automate" in name),
                next(iter(build_ach_map)),
            )
            target_ach_id = build_ach_map[target_ach_name]

            target_item_name = next(
                (name for name in item_map if "assembling-machine" in name),
                next(iter(item_map)),
            )
            target_item_id = item_map[target_item_name]

            # Add target_item_name and scenario telemetry hook to control.lua inside mp_test.zip
            mp_proxy.augment_save_freeplay_created_items(
                save_zip,
                {target_item_name: 1},
                enable_telemetry=True,
            )

            # Step 3: Start UDP Lockstep Proxy
            proxy = mp_proxy.FactorioLockstepProxy(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                server_host="127.0.0.1",
                server_port=server_port,
            )
            proxy.start()

            server_proc: subprocess.Popen[str] | None = None
            client_proc: subprocess.Popen[str] | None = None
            try:
                # Step 4: Launch headless MP server inside Podman with host networking
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

                # Step 5: Launch displayless client (SDL_VIDEODRIVER=offscreen) connected to Proxy
                client_cfg = client_dir / "config.ini"
                client_cfg.write_text(
                    f"[path]\nread-data={factorio_root}/data\nwrite-data={client_dir}\n"
                    f"[other]\ncheck_updates=false\n"
                )
                (client_dir / "player-data.json").write_text(
                    json.dumps({"service-username": "factorilog-bot"}, indent=2)
                )

                # Run client with isolated HOME so SteamUserStats does not pre-populate
                # achievements from the host user's personal Steam session.
                client_env = {
                    **os.environ,
                    "HOME": str(client_home),
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

                # Step 6: Closed-loop controller:
                # 1. Discover nearest mineral patch from factorilog_state.json
                # 2. Walk character to within mining reach of the patch
                # 3. Select the resource tile and mine ore into player inventory
                # 4. Pipette + Build assembling machine to unlock native achievement
                state_file = server_dir / "script-output" / "factorilog_state.json"
                res_name: str | None = None
                res_x = 0.0
                res_y = 0.0
                initial_ore = 0
                mined_ore = 0
                mining_phase = False
                build_queued = False
                last_cmd_time = 0.0
                last_state: mp_proxy.PlayerWorldState | None = None

                success = False
                join_deadline = time.monotonic() + 35.0
                while time.monotonic() < join_deadline:
                    state = mp_proxy.read_world_state(state_file)
                    if state is not None and proxy.stats.empty_client_heartbeats_seen >= 5:
                        last_state = state
                        if res_name is None and state.nearest_resource is not None:
                            res_name = state.nearest_resource.name
                            res_x = state.nearest_resource.x
                            res_y = state.nearest_resource.y
                            initial_ore = state.inventory.get(res_name, 0)

                        if res_name is not None:
                            mined_ore = state.inventory.get(res_name, 0) - initial_ore
                            dist = math.hypot(res_x - state.player_x, res_y - state.player_y)
                            now = time.monotonic()

                            if mined_ore < 1:
                                if not mining_phase and dist > 1.75:
                                    if (
                                        proxy.pending_action_count() == 0
                                        and (now - last_cmd_time) >= 0.25
                                    ):
                                        dx, dy = mp_proxy.compute_walking_vector(
                                            state.player_x, state.player_y, res_x, res_y
                                        )
                                        act_w, pay_w = mp_proxy.build_start_walking_action(dx, dy)
                                        proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                        last_cmd_time = now
                                else:
                                    if not mining_phase:
                                        mining_phase = True
                                        act_sw, pay_sw = mp_proxy.build_stop_walking_action()
                                        act_sel, pay_sel = mp_proxy.build_selected_entity_action(
                                            res_x, res_y
                                        )
                                        act_bm, pay_bm = mp_proxy.build_begin_mining_action()
                                        proxy.queue_action(act_sw, player_index=0, payload=pay_sw)
                                        proxy.queue_action(act_sel, player_index=0, payload=pay_sel)
                                        proxy.queue_action(act_bm, player_index=0, payload=pay_bm)
                                        last_cmd_time = now
                                    elif (
                                        not state.mining
                                        and proxy.pending_action_count() == 0
                                        and (now - last_cmd_time) >= 0.5
                                    ):
                                        if dist > 2.2:
                                            mining_phase = False
                                        else:
                                            act_sel, pay_sel = (
                                                mp_proxy.build_selected_entity_action(res_x, res_y)
                                            )
                                            act_bm, pay_bm = mp_proxy.build_begin_mining_action()
                                            proxy.queue_action(
                                                act_sel, player_index=0, payload=pay_sel
                                            )
                                            proxy.queue_action(
                                                act_bm, player_index=0, payload=pay_bm
                                            )
                                            last_cmd_time = now
                            elif not build_queued:
                                build_queued = True
                                act_sm, pay_sm = mp_proxy.build_stop_mining_action()
                                act_pip, pay_pip = mp_proxy.build_pipette_item_action(
                                    target_item_id
                                )
                                act_bld, pay_bld = mp_proxy.build_place_entity_action(
                                    state.player_x + 2.5,
                                    state.player_y,
                                    direction=0,
                                )
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                proxy.queue_action(act_sm, player_index=0, payload=pay_sm)
                                proxy.queue_action(act_pip, player_index=0, payload=pay_pip)
                                proxy.queue_action(act_bld, player_index=0, payload=pay_bld)
                                proxy.queue_action(act_clr, player_index=0, payload=pay_clr)

                    if (
                        mined_ore >= 1
                        and target_ach_id in proxy.stats.client_emitted_achievement_ids
                        and target_ach_id in proxy.stats.server_broadcast_achievement_ids
                    ):
                        success = True
                        break
                    time.sleep(0.05)

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
                self.assertIsNotNone(
                    res_name,
                    f"Telemetry did not report a nearest mineral patch.\n"
                    f"Last state: {last_state}\nServer log:\n{server_log}",
                )
                self.assertGreaterEqual(
                    mined_ore,
                    1,
                    f"Bot did not finish mining mineral patch ({res_name} at {res_x}, {res_y}).\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}\n"
                    f"Client log:\n{client_log}\nServer log:\n{server_log}",
                )
                self.assertTrue(
                    success,
                    f"Client did not naturally emit AchievementGained ({target_ach_name}={target_ach_id}) "
                    f"after finding/mining {res_name} and Pipette+Build actions.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}\n"
                    f"Client log:\n{client_log}\nServer log:\n{server_log}",
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
