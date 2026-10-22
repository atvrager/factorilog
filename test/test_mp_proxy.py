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

        act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(into_entity=True)
        self.assertEqual(act_ft_in, mp_proxy.ACTION_FAST_ENTITY_TRANSFER)
        self.assertEqual(pay_ft_in, b"\x01")

        act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(into_entity=False)
        self.assertEqual(act_ft_out, mp_proxy.ACTION_FAST_ENTITY_TRANSFER)
        self.assertEqual(pay_ft_out, b"\x00")

        act_fs, pay_fs = mp_proxy.build_fast_entity_split_action(into_entity=True)
        self.assertEqual(act_fs, mp_proxy.ACTION_FAST_ENTITY_SPLIT)
        self.assertEqual(pay_fs, b"\x01")

        act_res, pay_res = mp_proxy.build_start_research_action(15)
        self.assertEqual(act_res, mp_proxy.ACTION_START_RESEARCH)
        self.assertEqual(struct.unpack("<H", pay_res), (15,))

    def test_lazy_bastard_max_hand_craft_invariant(self) -> None:
        """Verify dynamic extraction and strict enforcement of the lazy-bastard max hand-craft limit."""
        fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        max_crafts = mp_proxy.extract_lazy_bastard_limit(raw_data)
        self.assertGreater(max_crafts, 0)

        proxy = mp_proxy.FactorioLockstepProxy(
            listen_host="127.0.0.1",
            listen_port=_get_free_udp_port(),
            server_host="127.0.0.1",
            server_port=_get_free_udp_port(),
            max_hand_crafts=max_crafts,
        )

        # Queuing within the budget succeeds
        act_c1, pay_c1 = mp_proxy.build_craft_action(recipe_id=10, count=max_crafts - 1)
        proxy.queue_action(act_c1, player_index=0, payload=pay_c1)
        self.assertEqual(proxy.pending_action_count(), 1)

        # Queuing the final allowed hand-craft succeeds
        act_c2, pay_c2 = mp_proxy.build_craft_action(recipe_id=10, count=1)
        proxy.queue_action(act_c2, player_index=0, payload=pay_c2)
        self.assertEqual(proxy.pending_action_count(), 2)

        # Queuing one more hand-craft beyond max_crafts raises ValueError and increments blocked count
        act_over, pay_over = mp_proxy.build_craft_action(recipe_id=10, count=1)
        with self.assertRaises(ValueError):
            proxy.queue_action(act_over, player_index=0, payload=pay_over)
        self.assertEqual(proxy.stats.blocked_hand_craft_actions, 1)
        self.assertEqual(proxy.pending_action_count(), 2)

        # Also verify wire-level scanning and stripping of client-emitted Craft actions
        hb_flags = (
            mp_proxy.HB_HAS_TICK_CLOSURES
            | mp_proxy.HB_HAS_SINGLE_TICK_CLOSURE
            | mp_proxy.HB_ALL_TICK_CLOSURES_EMPTY
        )
        empty_pkt = (
            bytes([mp_proxy.MSG_CLIENT_TO_SERVER_HEARTBEAT, hb_flags])
            + struct.pack("<I", 50)
            + struct.pack("<Q", 900)
            + struct.pack("<Q", 900)
        )
        craft_pkt = mp_proxy.inject_action_into_empty_client_heartbeat(
            empty_pkt,
            action_type=mp_proxy.ACTION_CRAFT,
            player_index=0,
            action_payload=pay_over,
        )
        assert craft_pkt is not None
        self.assertEqual(mp_proxy.scan_heartbeat_for_crafts(craft_pkt, 1), [(10, 1)])
        stripped_craft = mp_proxy.strip_client_cancellation_actions(
            craft_pkt, frozenset({mp_proxy.ACTION_CRAFT})
        )
        self.assertEqual(stripped_craft, empty_pkt)

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
        """Verify normalized 2D walking vector calculation and unit norm bound."""
        dx, dy = mp_proxy.compute_walking_vector(0.0, 0.0, 3.0, 4.0)
        self.assertAlmostEqual(dx, 0.6)
        self.assertAlmostEqual(dy, 0.8)
        self.assertLessEqual(dx * dx + dy * dy, 1.0)
        self.assertEqual(mp_proxy.compute_walking_vector(5.0, 5.0, 5.0, 5.0), (0.0, 0.0))

        # Verify that no angle or distance ever exceeds 1.0 in IEEE-754 float64
        for step in range(360):
            angle = math.radians(step * 1.0)
            for radius in (0.3, 1.1, 3.605551275463989, 7.3, 19.7):
                tx = radius * math.cos(angle)
                ty = radius * math.sin(angle)
                vx, vy = mp_proxy.compute_walking_vector(0.125, -0.375, tx, ty)
                self.assertLessEqual(
                    vx * vx + vy * vy,
                    1.0,
                    f"Vector squared norm exceeded 1.0 for angle={step}, radius={radius}",
                )


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
            recipe_map = id_mappings.get("recipe", {})
            self.assertTrue(
                build_ach_map,
                f"Failed to extract build-entity-achievement mappings from save: {list(id_mappings)}",
            )
            self.assertTrue(
                item_map,
                f"Failed to extract item mappings from save: {list(id_mappings)}",
            )
            self.assertTrue(
                recipe_map,
                f"Failed to extract recipe mappings from save: {list(id_mappings)}",
            )

            fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
            fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
            max_hand_crafts = mp_proxy.extract_lazy_bastard_limit(fixture_data)

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

            furnace_item_name = next(
                (name for name in item_map if "stone-furnace" in name),
                next(iter(item_map)),
            )
            furnace_item_id = item_map[furnace_item_name]

            fuel_item_name = next(
                (name for name in item_map if name == "coal"),
                next(iter(item_map)),
            )
            fuel_item_id = item_map[fuel_item_name]

            smelt_ore_name = next(
                (name for name in item_map if name == "iron-ore"),
                next(iter(item_map)),
            )
            smelt_ore_id = item_map[smelt_ore_name]

            craft_recipe_name = next(
                (name for name in recipe_map if name == "iron-gear-wheel"),
                next(iter(recipe_map)),
            )
            craft_recipe_id = recipe_map[craft_recipe_name]

            # Add starter items, scenario telemetry hook, and timelapse screenshot hook to control.lua
            mp_proxy.augment_save_freeplay_created_items(
                save_zip,
                {
                    target_item_name: 1,
                    furnace_item_name: 1,
                    fuel_item_name: 5,
                    smelt_ore_name: 2,
                },
                enable_telemetry=True,
                timelapse_interval_ticks=4,
            )

            # Step 3: Start UDP Lockstep Proxy with lazy-bastard max_hand_crafts limit
            proxy = mp_proxy.FactorioLockstepProxy(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                server_host="127.0.0.1",
                server_port=server_port,
                max_hand_crafts=max_hand_crafts,
            )
            proxy.start()

            live_mp4 = work_dir / "videos" / "bot_live_hud.mp4"
            timelapse_mp4 = work_dir / "videos" / "bot_timelapse.mp4"
            recorder = mp_proxy.XvfbVideoRecorder(live_mp4, width=1280, height=720, fps=30)
            has_xvfb_ffmpeg = bool(shutil.which("Xvfb") and shutil.which("ffmpeg"))
            if has_xvfb_ffmpeg:
                recorder.start_display()
                recorder.start_recording()

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

                # Step 5: Launch client connected to Proxy (on Xvfb display when available)
                client_cfg = client_dir / "config.ini"
                client_cfg.write_text(
                    f"[path]\nread-data={factorio_root}/data\nwrite-data={client_dir}\n"
                    f"[other]\ncheck_updates=false\n"
                    f"[graphics]\nfull-screen=false\npreferred-screen-index=0\n"
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
                    "DISPLAY": recorder.display if has_xvfb_ffmpeg else "",
                    "WAYLAND_DISPLAY": "",
                    "SDL_VIDEODRIVER": "x11" if has_xvfb_ffmpeg else "offscreen",
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
                    "--window-size",
                    "1280x720",
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
                # 4. Place stone-furnace, load fuel + iron-ore through FastEntityTransfer(into_entity=True)
                # 5. Extract smelted iron-plate through FastEntityTransfer(into_entity=False) and hand-craft
                #    iron-gear-wheel within the lazy-bastard max_hand_crafts invariant
                # 6. Pipette + Build assembling machine to unlock native achievement
                state_file = server_dir / "script-output" / "factorilog_state.json"
                res_name: str | None = None
                res_x = 0.0
                res_y = 0.0
                patrol_x = -12.0
                patrol_y = -8.0
                patrol_done = False
                furnace_x = 0.0
                furnace_y = 0.0
                initial_ore = 0
                mined_ore = 0
                mining_phase = False
                smelt_queued = False
                smelt_started_at = 0.0
                extract_and_craft_queued = False
                build_queued = False
                last_cmd_time = 0.0
                last_state: mp_proxy.PlayerWorldState | None = None

                success = False
                join_deadline = time.monotonic() + 55.0
                while time.monotonic() < join_deadline:
                    state = mp_proxy.read_world_state(state_file)
                    if state is not None and proxy.stats.empty_client_heartbeats_seen >= 90:
                        last_state = state
                        if res_name is None and state.nearest_resource is not None:
                            res_name = state.nearest_resource.name
                            res_x = state.nearest_resource.x
                            res_y = state.nearest_resource.y
                            initial_ore = state.inventory.get(res_name, 0)

                        if res_name is not None:
                            mined_ore = state.inventory.get(res_name, 0) - initial_ore
                            dist = math.hypot(res_x - state.player_x, res_y - state.player_y)
                            patrol_dist = math.hypot(
                                patrol_x - state.player_x, patrol_y - state.player_y
                            )
                            now = time.monotonic()

                            if not patrol_done:
                                if patrol_dist > 1.5:
                                    if (
                                        proxy.pending_action_count() == 0
                                        and (now - last_cmd_time) >= 0.25
                                    ):
                                        dx, dy = mp_proxy.compute_walking_vector(
                                            state.player_x, state.player_y, patrol_x, patrol_y
                                        )
                                        act_w, pay_w = mp_proxy.build_start_walking_action(dx, dy)
                                        proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                        last_cmd_time = now
                                else:
                                    patrol_done = True
                            elif mined_ore < 1:
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
                            elif not smelt_queued and proxy.pending_action_count() == 0:
                                smelt_queued = True
                                smelt_started_at = now
                                furnace_x = state.player_x
                                furnace_y = state.player_y - 2.0
                                act_sm, pay_sm = mp_proxy.build_stop_mining_action()
                                act_pip_f, pay_pip_f = mp_proxy.build_pipette_item_action(
                                    furnace_item_id
                                )
                                act_bld_f, pay_bld_f = mp_proxy.build_place_entity_action(
                                    furnace_x, furnace_y, direction=0
                                )
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_f, pay_sel_f = mp_proxy.build_selected_entity_action(
                                    furnace_x, furnace_y
                                )
                                act_pip_c, pay_pip_c = mp_proxy.build_pipette_item_action(
                                    fuel_item_id
                                )
                                act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=True
                                )
                                act_pip_o, pay_pip_o = mp_proxy.build_pipette_item_action(
                                    smelt_ore_id
                                )
                                for act, pay in (
                                    (act_sm, pay_sm),
                                    (act_pip_f, pay_pip_f),
                                    (act_bld_f, pay_bld_f),
                                    (act_clr, pay_clr),
                                    (act_sel_f, pay_sel_f),
                                    (act_pip_c, pay_pip_c),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f, pay_sel_f),
                                    (act_pip_o, pay_pip_o),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                smelt_queued
                                and not extract_and_craft_queued
                                and proxy.pending_action_count() == 0
                                and (now - smelt_started_at) >= 8.0
                            ):
                                extract_and_craft_queued = True
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_f, pay_sel_f = mp_proxy.build_selected_entity_action(
                                    furnace_x, furnace_y
                                )
                                act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=False
                                )
                                act_craft, pay_craft = mp_proxy.build_craft_action(
                                    craft_recipe_id, count=1
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_sel_f, pay_sel_f),
                                    (act_ft_out, pay_ft_out),
                                    (act_craft, pay_craft),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                extract_and_craft_queued
                                and not build_queued
                                and proxy.pending_action_count() == 0
                                and state.hand_crafted_count >= 1
                            ):
                                build_queued = True
                                act_pip, pay_pip = mp_proxy.build_pipette_item_action(
                                    target_item_id
                                )
                                act_bld, pay_bld = mp_proxy.build_place_entity_action(
                                    state.player_x + 2.5,
                                    state.player_y,
                                    direction=0,
                                )
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                proxy.queue_action(act_pip, player_index=0, payload=pay_pip)
                                proxy.queue_action(act_bld, player_index=0, payload=pay_bld)
                                proxy.queue_action(act_clr, player_index=0, payload=pay_clr)

                    if (
                        mined_ore >= 1
                        and last_state is not None
                        and last_state.hand_crafted_count >= 1
                        and target_ach_id in proxy.stats.client_emitted_achievement_ids
                        and target_ach_id in proxy.stats.server_broadcast_achievement_ids
                    ):
                        # Allow a brief pause so the achievement toast and final frames render
                        time.sleep(0.5)
                        success = True
                        break
                    time.sleep(0.05)

                if has_xvfb_ffmpeg:
                    recorder.stop()
                    self.assertTrue(live_mp4.is_file(), "Live HUD MP4 was not created")
                    self.assertGreater(live_mp4.stat().st_size, 1024, "Live HUD MP4 is empty")

                    frames_dir = client_dir / "script-output" / "timelapse"
                    if frames_dir.is_dir() and any(frames_dir.glob("frame_*.jpg")):
                        mp_proxy.encode_timelapse_mp4(frames_dir, timelapse_mp4, fps=15)
                        self.assertTrue(timelapse_mp4.is_file(), "Timelapse MP4 was not created")
                        self.assertGreater(
                            timelapse_mp4.stat().st_size, 1024, "Timelapse MP4 is empty"
                        )

                    export_dir = os.environ.get("FACTORILOG_VIDEO_EXPORT_DIR")
                    if export_dir:
                        exp_path = Path(export_dir)
                        exp_path.mkdir(parents=True, exist_ok=True)
                        if live_mp4.is_file():
                            shutil.copy2(live_mp4, exp_path / "bot_live_hud.mp4")
                        if timelapse_mp4.is_file():
                            shutil.copy2(timelapse_mp4, exp_path / "bot_timelapse.mp4")

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
                self.assertIsNotNone(last_state)
                assert last_state is not None
                self.assertGreaterEqual(
                    last_state.hand_crafted_count,
                    1,
                    f"Bot did not smelt iron-plate and hand-craft {craft_recipe_name}.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}",
                )
                self.assertLessEqual(
                    last_state.hand_crafted_count,
                    max_hand_crafts,
                    f"Server hand_crafted_count exceeded lazy-bastard limit ({max_hand_crafts}).",
                )
                self.assertLessEqual(
                    proxy.stats.hand_crafted_count,
                    max_hand_crafts,
                    f"Proxy hand_crafted_count exceeded lazy-bastard limit ({max_hand_crafts}).",
                )
                self.assertEqual(proxy.stats.blocked_hand_craft_actions, 0)
                self.assertTrue(
                    success,
                    f"Client did not naturally emit AchievementGained ({target_ach_name}={target_ach_id}) "
                    f"after finding/mining {res_name}, smelting, hand-crafting, and Pipette+Build actions.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}\n"
                    f"Client log:\n{client_log}\nServer log:\n{server_log}",
                )
            finally:
                recorder.stop()
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
