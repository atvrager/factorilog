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
import solve
from foundry import mp_proxy
from foundry.campaign.bot_agent import (
    BotAgent,
    encode_assembler_batch_actions,
    verify_placed_entities,
)

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

        act_opn, pay_opn = mp_proxy.build_open_gui_action()
        self.assertEqual(act_opn, mp_proxy.ACTION_OPEN_GUI)
        self.assertEqual(pay_opn, b"")

        act_cls, pay_cls = mp_proxy.build_close_gui_action()
        self.assertEqual(act_cls, mp_proxy.ACTION_CLOSE_GUI)
        self.assertEqual(pay_cls, b"\x00\x00")

        act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
        self.assertEqual(act_clr, mp_proxy.ACTION_CLEAR_CURSOR)
        self.assertEqual(pay_clr, b"")

        act_rst, pay_rst = mp_proxy.build_reset_assembling_machine_action()
        self.assertEqual(act_rst, mp_proxy.ACTION_RESET_ASSEMBLING_MACHINE)
        self.assertEqual(pay_rst, b"")

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
            research_ach_map = id_mappings.get("research-with-science-pack-achievement", {})
            item_map = id_mappings.get("item", {})
            recipe_map = id_mappings.get("recipe", {})
            tech_map = id_mappings.get("technology", {})
            quality_map = id_mappings.get("quality", {})
            self.assertTrue(
                build_ach_map,
                f"Failed to extract build-entity-achievement mappings from save: {list(id_mappings)}",
            )
            self.assertTrue(
                research_ach_map,
                f"Failed to extract research-with-science-pack-achievement mappings: {list(id_mappings)}",
            )
            self.assertTrue(
                item_map,
                f"Failed to extract item mappings from save: {list(id_mappings)}",
            )
            self.assertTrue(
                recipe_map,
                f"Failed to extract recipe mappings from save: {list(id_mappings)}",
            )
            self.assertTrue(
                tech_map,
                f"Failed to extract technology mappings from save: {list(id_mappings)}",
            )

            fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
            fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
            max_hand_crafts = mp_proxy.extract_lazy_bastard_limit(fixture_data)

            # Dynamically resolve target achievement IDs and placeable entity item IDs
            target_ach_name = next(
                (name for name in build_ach_map if "automate" in name),
                next(iter(build_ach_map)),
            )
            target_ach_id = build_ach_map[target_ach_name]

            research_ach_name = next(
                (name for name in research_ach_map if "automation" in name),
                next(iter(research_ach_map)),
            )
            research_ach_id = research_ach_map[research_ach_name]

            automation_tech_name = next(
                (name for name in tech_map if name == "automation"),
                next(iter(tech_map)),
            )
            automation_tech_id = tech_map[automation_tech_name]

            target_item_name = next(
                (name for name in item_map if name == "assembling-machine-1"),
                next(
                    (name for name in item_map if "assembling-machine" in name),
                    next(iter(item_map)),
                ),
            )
            target_item_id = item_map[target_item_name]
            asm_recipe_id = recipe_map[target_item_name]

            furnace_item_name = next(
                (name for name in item_map if "stone-furnace" in name),
                next(iter(item_map)),
            )
            furnace_item_id = item_map[furnace_item_name]
            furnace_recipe_id = recipe_map[furnace_item_name]

            drill_item_name = next(
                (name for name in item_map if name == "burner-mining-drill"),
                next(iter(item_map)),
            )

            pump_item_name = next(
                (name for name in item_map if name == "offshore-pump"),
                next(iter(item_map)),
            )
            pump_item_id = item_map[pump_item_name]
            pump_recipe_id = recipe_map[pump_item_name]

            boiler_item_name = next(
                (name for name in item_map if name == "boiler"),
                next(iter(item_map)),
            )
            boiler_item_id = item_map[boiler_item_name]
            boiler_recipe_id = recipe_map[boiler_item_name]

            engine_item_name = next(
                (name for name in item_map if name == "steam-engine"),
                next(iter(item_map)),
            )
            engine_item_id = item_map[engine_item_name]
            engine_recipe_id = recipe_map[engine_item_name]

            pole_item_name = next(
                (name for name in item_map if name == "small-electric-pole"),
                next(iter(item_map)),
            )
            pole_item_id = item_map[pole_item_name]
            pole_recipe_id = recipe_map[pole_item_name]

            lab_item_name = next(
                (name for name in item_map if name == "lab"),
                next(iter(item_map)),
            )
            lab_item_id = item_map[lab_item_name]
            lab_recipe_id = recipe_map[lab_item_name]

            stone_item_name = next(
                (name for name in item_map if name == "stone"),
                next(iter(item_map)),
            )

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

            copper_ore_name = next(
                (name for name in item_map if name == "copper-ore"),
                next(iter(item_map)),
            )
            copper_ore_id = item_map[copper_ore_name]

            plate_item_name = next(
                (name for name in item_map if name == "iron-plate"),
                next(iter(item_map)),
            )

            copper_plate_name = next(
                (name for name in item_map if name == "copper-plate"),
                next(iter(item_map)),
            )

            craft_recipe_name = next(
                (name for name in recipe_map if name == "iron-gear-wheel"),
                next(iter(recipe_map)),
            )

            belt_item_name = next(
                (name for name in item_map if name == "transport-belt"),
                next(iter(item_map)),
            )

            cable_item_name = next(
                (name for name in item_map if name == "copper-cable"),
                next(iter(item_map)),
            )

            circuit_item_name = next(
                (name for name in item_map if name == "electronic-circuit"),
                next(iter(item_map)),
            )

            science_item_name = next(
                (name for name in item_map if name == "automation-science-pack"),
                next(iter(item_map)),
            )
            science_item_id = item_map[science_item_name]
            science_recipe_id = recipe_map[science_item_name]

            inserter_item_name = next(
                (name for name in item_map if name == "inserter"),
                next(iter(item_map)),
            )

            normal_quality_id = quality_map.get("normal", 1)
            game_data = solve.Data(str(fixture_path))

            # Pre-scan clean seed-42 Freeplay cartography so the exact synthesized BOM
            # can be supplied when Phase 0 Lazy Bastard bootstrap + BOM fabrication proof finishes.
            scan_zip = server_dir / "saves" / "mp_scan.zip"
            shutil.copy2(save_zip, scan_zip)
            mp_proxy.augment_save_freeplay_created_items(
                scan_zip,
                {},
                enable_telemetry=True,
            )
            cmd_scan = [
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
                "--benchmark",
                "/work/server/saves/mp_scan.zip",
                "--benchmark-ticks",
                "5",
            ]
            res_scan = subprocess.run(
                cmd_scan, env=podman_env, capture_output=True, text=True, check=False
            )
            self.assertEqual(
                res_scan.returncode,
                0,
                f"Cartography pre-scan in Podman failed:\n{res_scan.stdout}\n{res_scan.stderr}",
            )
            cartography_file = server_dir / "script-output" / "factorilog_cartography.json"
            self.assertTrue(cartography_file.is_file())
            bot_agent = BotAgent(
                data=game_data,
                researched_techs={"automation", "logistics"},
            )
            self.assertTrue(bot_agent.ingest_cartography(cartography_file))
            pre_plan = bot_agent.synthesize_plan(
                reference_pos=(0.0, 0.0),
                bounds=None,
                base_origin=None,
                verify=False,
            )
            phase1_bom_items = dict(pre_plan.bill_of_materials)
            phase1_bom_items[fuel_item_name] = phase1_bom_items.get(fuel_item_name, 0) + 50

            # Add scenario telemetry hook and timelapse screenshot hook with zero starting items
            mp_proxy.augment_save_freeplay_created_items(
                save_zip,
                {},
                enable_telemetry=True,
                timelapse_interval_ticks=240,
                stationary_game_speed=16.0,
                walking_game_speed=1.0,
                phase1_bom_items=phase1_bom_items,
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
            recorder = mp_proxy.XvfbVideoRecorder(
                live_mp4, width=1280, height=720, fps=30, playback_slowdown=1.5
            )
            has_xvfb_ffmpeg = bool(shutil.which("Xvfb") and shutil.which("ffmpeg"))
            if has_xvfb_ffmpeg:
                recorder.start_display()

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
                # 1. Discover nearest mineral patch and shoreline site from factorilog_state.json
                # 2. Walk character to within mining reach of the patch and mine ore
                # 3. Walk smoothly to shoreline_site.stand_x, shoreline_site.stand_y
                # 4. Build the lakeside steam power chain and stone-furnace, then load fuel and ore
                # 5. Extract smelted iron-plate and hand-craft 1 iron-gear-wheel within the limit
                # 6. Build assembling-machine-1, set its recipe, feed iron-plate, and extract output
                state_file = server_dir / "script-output" / "factorilog_state.json"
                mining_targets = (
                    (smelt_ore_name, 156),
                    (stone_item_name, 25),
                    (fuel_item_name, 64),
                    (copper_ore_name, 42),
                )
                mined_resources: dict[str, int] = {
                    smelt_ore_name: 0,
                    stone_item_name: 0,
                    fuel_item_name: 0,
                    copper_ore_name: 0,
                }
                patch_idx = 0
                furnaces_hand_crafted = False
                smelt_start_tick = 0
                power_craft_queued = False
                power_placed_queued = False
                lab_craft_queued = False
                lab_place_and_science_queued = False
                final_science_load_queued = False
                asm_craft_queued = False
                asm_place_and_gears_queued = False
                phase1_cables_queued = False
                phase1_circuits_queued = False
                phase1_belts_queued = False
                phase1_inserters_queued = False
                phase1_asm_queued = False
                phase1_collected_queued = False
                phase1_inventory_ready = False
                phase1_planned = False
                phase1_target_task_indices: set[int] = set()
                phase1_gears_mc_target = 9
                phase1_cables_mc_target = 16
                phase1_circuits_mc_target = 21
                phase1_belts_mc_target = 23
                phase1_inserters_mc_target = 25
                phase1_asm_mc_target = 26
                phase1_asm_start_tick = 0
                res_name: str | None = None
                res_x = 0.0
                res_y = 0.0
                site: mp_proxy.ShorelineSite | None = None
                primed_walk = False
                primed_stop = False
                recording_started = False
                walk_stage = 0
                walk_dx = -1.0
                walk_dy = 0.0
                shore_stage = 0
                shore_dx = 0.0
                shore_dy = 0.0
                shore_arrived = False
                mined_ore = 0
                mining_phase = False
                smelt_queued = False
                last_cmd_time = 0.0
                last_state: mp_proxy.PlayerWorldState | None = None
                braking_throttles = (
                    0.88,
                    0.77,
                    0.67,
                    0.59,
                    0.51,
                    0.45,
                    0.39,
                    0.34,
                    0.30,
                    0.26,
                    0.23,
                    0.20,
                    0.18,
                    0.15,
                    0.13,
                    0.11,
                )

                success = False
                join_deadline = time.monotonic() + 275.0
                while time.monotonic() < join_deadline:
                    state = mp_proxy.read_world_state(state_file)
                    if state is not None and proxy.stats.empty_client_heartbeats_seen >= 90:
                        last_state = state
                        if res_name is None and smelt_ore_name in state.resources_by_name:
                            first_patch = state.resources_by_name[smelt_ore_name]
                            res_name = first_patch.name
                            res_x = first_patch.x
                            res_y = first_patch.y
                        if site is None and state.shoreline_site is not None:
                            site = state.shoreline_site

                        now = time.monotonic()
                        # Pre-recording setup (before tick 1200):
                        # Prime PlayerInputSource::lastSentWalkingState->tick > 0, then stand still
                        # across 3 full 300-tick windows (300..1200) so ServerSynchronizer
                        # drops client latency L all the way to 2 ticks.
                        if res_name is not None and not primed_stop:
                            raw_dx = res_x - state.player_x
                            raw_dy = res_y - state.player_y
                            short_err = abs(raw_dy) if abs(raw_dx) >= abs(raw_dy) else abs(raw_dx)
                            if not primed_walk and proxy.pending_action_count() == 0:
                                primed_walk = True
                                if abs(raw_dx) >= abs(raw_dy):
                                    pdx, pdy = 0.0, math.copysign(1.0, raw_dy)
                                else:
                                    pdx, pdy = math.copysign(1.0, raw_dx), 0.0
                                act_pw, pay_pw = mp_proxy.build_start_walking_action(pdx, pdy)
                                proxy.queue_action(act_pw, player_index=0, payload=pay_pw)
                                last_cmd_time = now
                            elif (
                                primed_walk
                                and proxy.pending_action_count() == 0
                                and (short_err <= 2.35 or state.tick >= 260)
                            ):
                                primed_stop = True
                                act_ps, pay_ps = mp_proxy.build_stop_walking_action()
                                proxy.queue_action(act_ps, player_index=0, payload=pay_ps)
                                last_cmd_time = now

                        # Wait until ServerSynchronizer reduces client latency at tick 1200
                        if (
                            res_name is not None
                            and site is not None
                            and primed_stop
                            and state.tick >= 1200
                        ):
                            if state.tick >= 1215 and not recording_started:
                                recording_started = True
                                if has_xvfb_ffmpeg:
                                    recorder.start_recording()

                            mined_ore = max(mined_ore, state.inventory.get(smelt_ore_name, 0))
                            for r_key in mined_resources:
                                mined_resources[r_key] = max(
                                    mined_resources[r_key], state.inventory.get(r_key, 0)
                                )

                            if patch_idx < len(mining_targets):
                                cur_res_name, cur_res_goal = mining_targets[patch_idx]
                                cur_patch = state.resources_by_name.get(cur_res_name)
                                if state.inventory.get(cur_res_name, 0) >= cur_res_goal:
                                    if proxy.pending_action_count() == 0:
                                        act_sm, pay_sm = mp_proxy.build_stop_mining_action()
                                        proxy.queue_action(act_sm, player_index=0, payload=pay_sm)
                                        if state.walking:
                                            act_sw, pay_sw = mp_proxy.build_stop_walking_action()
                                            proxy.queue_action(
                                                act_sw, player_index=0, payload=pay_sw
                                            )
                                        patch_idx += 1
                                        walk_stage = 0
                                        mining_phase = False
                                        last_cmd_time = now
                                elif cur_patch is not None:
                                    raw_dx = cur_patch.x - state.player_x
                                    raw_dy = cur_patch.y - state.player_y
                                    dist = math.hypot(raw_dx, raw_dy)
                                    if not mining_phase:
                                        if (
                                            dist <= 2.15
                                            and not state.walking
                                            and proxy.pending_action_count() == 0
                                        ):
                                            mining_phase = True
                                            act_sel, pay_sel = (
                                                mp_proxy.build_selected_entity_action(
                                                    cur_patch.x, cur_patch.y
                                                )
                                            )
                                            act_bm, pay_bm = mp_proxy.build_begin_mining_action()
                                            proxy.queue_action(
                                                act_sel, player_index=0, payload=pay_sel
                                            )
                                            proxy.queue_action(
                                                act_bm, player_index=0, payload=pay_bm
                                            )
                                            last_cmd_time = now
                                        elif (
                                            walk_stage == 0
                                            and not state.walking
                                            and proxy.pending_action_count() == 0
                                            and (now - last_cmd_time) >= 0.35
                                        ):
                                            walk_stage = 1
                                            if abs(raw_dx) >= abs(raw_dy):
                                                walk_dx, walk_dy = math.copysign(1.0, raw_dx), 0.0
                                            else:
                                                walk_dx, walk_dy = 0.0, math.copysign(1.0, raw_dy)
                                            act_w, pay_w = mp_proxy.build_start_walking_action(
                                                walk_dx, walk_dy
                                            )
                                            proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                            last_cmd_time = now
                                        elif (
                                            walk_stage == 1
                                            and state.walking
                                            and proxy.pending_action_count() == 0
                                            and (now - last_cmd_time) >= 0.2
                                        ):
                                            axis_err = (
                                                abs(raw_dx)
                                                if abs(walk_dx) >= abs(walk_dy)
                                                else abs(raw_dy)
                                            )
                                            signed_err = (
                                                raw_dx * walk_dx
                                                if abs(walk_dx) >= abs(walk_dy)
                                                else raw_dy * walk_dy
                                            )
                                            if (
                                                axis_err <= 2.35
                                                or signed_err <= 0.0
                                                or dist <= 2.15
                                            ):
                                                for throttle in braking_throttles:
                                                    act_b, pay_b = (
                                                        mp_proxy.build_start_walking_action(
                                                            walk_dx * throttle,
                                                            walk_dy * throttle,
                                                        )
                                                    )
                                                    proxy.queue_action(
                                                        act_b, player_index=0, payload=pay_b
                                                    )
                                                act_sw, pay_sw = (
                                                    mp_proxy.build_stop_walking_action()
                                                )
                                                proxy.queue_action(
                                                    act_sw, player_index=0, payload=pay_sw
                                                )
                                                if dist <= 2.35:
                                                    mining_phase = True
                                                    act_sel, pay_sel = (
                                                        mp_proxy.build_selected_entity_action(
                                                            cur_patch.x, cur_patch.y
                                                        )
                                                    )
                                                    act_bm, pay_bm = (
                                                        mp_proxy.build_begin_mining_action()
                                                    )
                                                    proxy.queue_action(
                                                        act_sel, player_index=0, payload=pay_sel
                                                    )
                                                    proxy.queue_action(
                                                        act_bm, player_index=0, payload=pay_bm
                                                    )
                                                else:
                                                    walk_stage = 2
                                                last_cmd_time = now
                                        elif (
                                            walk_stage == 1
                                            and not state.walking
                                            and proxy.pending_action_count() == 0
                                            and (now - last_cmd_time) >= 0.8
                                        ):
                                            if abs(raw_dx) >= abs(raw_dy):
                                                walk_dx, walk_dy = (
                                                    math.copysign(1.0, raw_dx),
                                                    0.0,
                                                )
                                            else:
                                                walk_dx, walk_dy = (
                                                    0.0,
                                                    math.copysign(1.0, raw_dy),
                                                )
                                            act_w, pay_w = mp_proxy.build_start_walking_action(
                                                walk_dx, walk_dy
                                            )
                                            proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                            last_cmd_time = now
                                        elif (
                                            walk_stage == 2
                                            and not state.walking
                                            and proxy.pending_action_count() == 0
                                            and (now - last_cmd_time) >= 0.4
                                        ):
                                            walk_stage = 0
                                            last_cmd_time = now
                                    elif (
                                        mining_phase
                                        and not state.mining
                                        and not state.walking
                                        and proxy.pending_action_count() == 0
                                        and (now - last_cmd_time) >= 0.6
                                    ):
                                        if dist > 2.55:
                                            mining_phase = False
                                            walk_stage = 0
                                        else:
                                            act_sel, pay_sel = (
                                                mp_proxy.build_selected_entity_action(
                                                    cur_patch.x, cur_patch.y
                                                )
                                            )
                                            act_bm, pay_bm = mp_proxy.build_begin_mining_action()
                                            proxy.queue_action(
                                                act_sel, player_index=0, payload=pay_sel
                                            )
                                            proxy.queue_action(
                                                act_bm, player_index=0, payload=pay_bm
                                            )
                                            last_cmd_time = now
                            elif not shore_arrived:
                                s_dx = site.stand_x - state.player_x
                                s_dy = site.stand_y - state.player_y
                                s_dist = math.hypot(s_dx, s_dy)
                                if (
                                    shore_stage == 0
                                    and not state.walking
                                    and proxy.pending_action_count() == 0
                                    and (now - last_cmd_time) >= 0.4
                                ):
                                    act_sm, pay_sm = mp_proxy.build_stop_mining_action()
                                    proxy.queue_action(act_sm, player_index=0, payload=pay_sm)
                                    if s_dist <= 1.85:
                                        shore_arrived = True
                                        last_cmd_time = now
                                    else:
                                        shore_stage = 1
                                        if abs(s_dx) >= abs(s_dy):
                                            shore_dx, shore_dy = math.copysign(1.0, s_dx), 0.0
                                        else:
                                            shore_dx, shore_dy = 0.0, math.copysign(1.0, s_dy)
                                        act_w, pay_w = mp_proxy.build_start_walking_action(
                                            shore_dx, shore_dy
                                        )
                                        proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                        last_cmd_time = now
                                elif (
                                    shore_stage == 1
                                    and state.walking
                                    and proxy.pending_action_count() == 0
                                    and (now - last_cmd_time) >= 0.25
                                ):
                                    err1 = (
                                        abs(s_dx) if abs(shore_dx) >= abs(shore_dy) else abs(s_dy)
                                    )
                                    signed_err1 = (
                                        s_dx * shore_dx
                                        if abs(shore_dx) >= abs(shore_dy)
                                        else s_dy * shore_dy
                                    )
                                    if err1 <= 2.35 or signed_err1 <= 0.0:
                                        for throttle in braking_throttles:
                                            act_b, pay_b = mp_proxy.build_start_walking_action(
                                                shore_dx * throttle, shore_dy * throttle
                                            )
                                            proxy.queue_action(act_b, player_index=0, payload=pay_b)
                                        act_sw, pay_sw = mp_proxy.build_stop_walking_action()
                                        proxy.queue_action(act_sw, player_index=0, payload=pay_sw)
                                        shore_stage = 2
                                        last_cmd_time = now
                                elif (
                                    shore_stage == 1
                                    and not state.walking
                                    and proxy.pending_action_count() == 0
                                    and (now - last_cmd_time) >= 0.8
                                ):
                                    if abs(s_dx) >= abs(s_dy):
                                        shore_dx, shore_dy = math.copysign(1.0, s_dx), 0.0
                                    else:
                                        shore_dx, shore_dy = 0.0, math.copysign(1.0, s_dy)
                                    act_w, pay_w = mp_proxy.build_start_walking_action(
                                        shore_dx, shore_dy
                                    )
                                    proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                    last_cmd_time = now
                                elif (
                                    shore_stage == 2
                                    and not state.walking
                                    and proxy.pending_action_count() == 0
                                    and (now - last_cmd_time) >= 0.5
                                ):
                                    b_dx = site.boiler_x - state.player_x
                                    b_dy = site.boiler_y - state.player_y
                                    clear_of_boiler = abs(b_dx) >= 1.65 or abs(b_dy) >= 2.15
                                    if (
                                        s_dist <= 2.15 or (abs(s_dx) <= 1.85 and abs(s_dy) <= 1.85)
                                    ) and clear_of_boiler:
                                        shore_arrived = True
                                        last_cmd_time = now
                                    else:
                                        shore_stage = 1
                                        if abs(s_dx) >= abs(s_dy):
                                            shore_dx, shore_dy = math.copysign(1.0, s_dx), 0.0
                                        else:
                                            shore_dx, shore_dy = 0.0, math.copysign(1.0, s_dy)
                                        act_w, pay_w = mp_proxy.build_start_walking_action(
                                            shore_dx, shore_dy
                                        )
                                        proxy.queue_action(act_w, player_index=0, payload=pay_w)
                                        last_cmd_time = now
                            elif (
                                shore_arrived
                                and not furnaces_hand_crafted
                                and not state.walking
                                and proxy.pending_action_count() == 0
                                and (now - last_cmd_time) >= 0.2
                            ):
                                furnaces_hand_crafted = True
                                act_cf, pay_cf = mp_proxy.build_craft_action(
                                    furnace_recipe_id, count=5
                                )
                                proxy.queue_action(act_cf, player_index=0, payload=pay_cf)
                            elif (
                                shore_arrived
                                and furnaces_hand_crafted
                                and not smelt_queued
                                and not state.walking
                                and proxy.pending_action_count() == 0
                                and state.inventory.get(furnace_item_name, 0) >= 6
                                and (now - last_cmd_time) >= 0.2
                            ):
                                smelt_queued = True
                                smelt_start_tick = state.tick
                                f4_x = 2.0 * site.furnace_x - site.furnace2_x
                                f4_y = 2.0 * site.furnace_y - site.furnace2_y
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=True
                                )
                                act_fs_in, pay_fs_in = mp_proxy.build_fast_entity_split_action(
                                    into_entity=True
                                )
                                act_pip_f, pay_pip_f = mp_proxy.build_pipette_item_action(
                                    furnace_item_id
                                )
                                act_bld_f1, pay_bld_f1 = mp_proxy.build_place_entity_action(
                                    site.furnace_x, site.furnace_y, direction=0
                                )
                                act_bld_f2, pay_bld_f2 = mp_proxy.build_place_entity_action(
                                    site.furnace2_x, site.furnace2_y, direction=0
                                )
                                act_bld_f3, pay_bld_f3 = mp_proxy.build_place_entity_action(
                                    site.furnace3_x, site.furnace3_y, direction=0
                                )
                                act_bld_f4, pay_bld_f4 = mp_proxy.build_place_entity_action(
                                    f4_x, f4_y, direction=0
                                )
                                act_sel_f1, pay_sel_f1 = mp_proxy.build_selected_entity_action(
                                    site.furnace_x, site.furnace_y
                                )
                                act_sel_f2, pay_sel_f2 = mp_proxy.build_selected_entity_action(
                                    site.furnace2_x, site.furnace2_y
                                )
                                act_sel_f3, pay_sel_f3 = mp_proxy.build_selected_entity_action(
                                    site.furnace3_x, site.furnace3_y
                                )
                                act_sel_f4, pay_sel_f4 = mp_proxy.build_selected_entity_action(
                                    f4_x, f4_y
                                )
                                act_pip_c, pay_pip_c = mp_proxy.build_pipette_item_action(
                                    fuel_item_id
                                )
                                act_pip_io, pay_pip_io = mp_proxy.build_pipette_item_action(
                                    smelt_ore_id
                                )
                                act_pip_co, pay_pip_co = mp_proxy.build_pipette_item_action(
                                    copper_ore_id
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_pip_f, pay_pip_f),
                                    (act_bld_f1, pay_bld_f1),
                                    (act_bld_f2, pay_bld_f2),
                                    (act_bld_f3, pay_bld_f3),
                                    (act_bld_f4, pay_bld_f4),
                                    (act_clr, pay_clr),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_pip_c, pay_pip_c),
                                    (act_fs_in, pay_fs_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_pip_c, pay_pip_c),
                                    (act_fs_in, pay_fs_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_pip_c, pay_pip_c),
                                    (act_fs_in, pay_fs_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_pip_c, pay_pip_c),
                                    (act_fs_in, pay_fs_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_pip_co, pay_pip_co),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                smelt_queued
                                and not power_craft_queued
                                and proxy.pending_action_count() == 0
                                and state.steam_power_researched
                                and state.electronics_researched
                            ):
                                power_craft_queued = True
                                f4_x = 2.0 * site.furnace_x - site.furnace2_x
                                f4_y = 2.0 * site.furnace_y - site.furnace2_y
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_f1, pay_sel_f1 = mp_proxy.build_selected_entity_action(
                                    site.furnace_x, site.furnace_y
                                )
                                act_sel_f2, pay_sel_f2 = mp_proxy.build_selected_entity_action(
                                    site.furnace2_x, site.furnace2_y
                                )
                                act_sel_f3, pay_sel_f3 = mp_proxy.build_selected_entity_action(
                                    site.furnace3_x, site.furnace3_y
                                )
                                act_sel_f4, pay_sel_f4 = mp_proxy.build_selected_entity_action(
                                    f4_x, f4_y
                                )
                                act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=False
                                )
                                act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=True
                                )
                                act_pip_io, pay_pip_io = mp_proxy.build_pipette_item_action(
                                    smelt_ore_id
                                )
                                act_cp, pay_cp = mp_proxy.build_craft_action(
                                    pump_recipe_id, count=1
                                )
                                act_cb, pay_cb = mp_proxy.build_craft_action(
                                    boiler_recipe_id, count=1
                                )
                                act_ce, pay_ce = mp_proxy.build_craft_action(
                                    engine_recipe_id, count=1
                                )
                                act_cpo, pay_cpo = mp_proxy.build_craft_action(
                                    pole_recipe_id, count=1
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_pip_io, pay_pip_io),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_cp, pay_cp),
                                    (act_cb, pay_cb),
                                    (act_ce, pay_ce),
                                    (act_cpo, pay_cpo),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                power_craft_queued
                                and not power_placed_queued
                                and proxy.pending_action_count() == 0
                                and state.inventory.get(pump_item_name, 0) >= 1
                                and state.inventory.get(boiler_item_name, 0) >= 1
                                and state.inventory.get(engine_item_name, 0) >= 1
                                and state.inventory.get(pole_item_name, 0) >= 2
                            ):
                                power_placed_queued = True
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=True
                                )
                                act_pip_p, pay_pip_p = mp_proxy.build_pipette_item_action(
                                    pump_item_id
                                )
                                act_bld_p, pay_bld_p = mp_proxy.build_place_entity_action(
                                    site.pump_x, site.pump_y, direction=site.pump_dir
                                )
                                act_pip_b, pay_pip_b = mp_proxy.build_pipette_item_action(
                                    boiler_item_id
                                )
                                act_bld_b, pay_bld_b = mp_proxy.build_place_entity_action(
                                    site.boiler_x, site.boiler_y, direction=site.boiler_dir
                                )
                                act_sel_b, pay_sel_b = mp_proxy.build_selected_entity_action(
                                    site.boiler_x, site.boiler_y
                                )
                                act_pip_c, pay_pip_c = mp_proxy.build_pipette_item_action(
                                    fuel_item_id
                                )
                                act_pip_e, pay_pip_e = mp_proxy.build_pipette_item_action(
                                    engine_item_id
                                )
                                act_bld_e, pay_bld_e = mp_proxy.build_place_entity_action(
                                    site.engine_x, site.engine_y, direction=site.engine_dir
                                )
                                act_pip_po, pay_pip_po = mp_proxy.build_pipette_item_action(
                                    pole_item_id
                                )
                                act_bld_po, pay_bld_po = mp_proxy.build_place_entity_action(
                                    site.pole_x, site.pole_y, direction=0
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_pip_p, pay_pip_p),
                                    (act_bld_p, pay_bld_p),
                                    (act_clr, pay_clr),
                                    (act_pip_b, pay_pip_b),
                                    (act_bld_b, pay_bld_b),
                                    (act_clr, pay_clr),
                                    (act_sel_b, pay_sel_b),
                                    (act_pip_c, pay_pip_c),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_pip_e, pay_pip_e),
                                    (act_bld_e, pay_bld_e),
                                    (act_clr, pay_clr),
                                    (act_pip_po, pay_pip_po),
                                    (act_bld_po, pay_bld_po),
                                    (act_clr, pay_clr),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                power_placed_queued
                                and not lab_craft_queued
                                and proxy.pending_action_count() == 0
                                and state.tick >= smelt_start_tick + 10800
                            ):
                                lab_craft_queued = True
                                f4_x = 2.0 * site.furnace_x - site.furnace2_x
                                f4_y = 2.0 * site.furnace_y - site.furnace2_y
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_f1, pay_sel_f1 = mp_proxy.build_selected_entity_action(
                                    site.furnace_x, site.furnace_y
                                )
                                act_sel_f2, pay_sel_f2 = mp_proxy.build_selected_entity_action(
                                    site.furnace2_x, site.furnace2_y
                                )
                                act_sel_f3, pay_sel_f3 = mp_proxy.build_selected_entity_action(
                                    site.furnace3_x, site.furnace3_y
                                )
                                act_sel_f4, pay_sel_f4 = mp_proxy.build_selected_entity_action(
                                    f4_x, f4_y
                                )
                                act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=False
                                )
                                act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=True
                                )
                                act_pip_io, pay_pip_io = mp_proxy.build_pipette_item_action(
                                    smelt_ore_id
                                )
                                act_clb, pay_clb = mp_proxy.build_craft_action(
                                    lab_recipe_id, count=1
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_ft_out, pay_ft_out),
                                    (act_clb, pay_clb),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                lab_craft_queued
                                and not lab_place_and_science_queued
                                and proxy.pending_action_count() == 0
                                and state.science_pack_researched
                                and state.inventory.get(lab_item_name, 0) >= 1
                            ):
                                lab_place_and_science_queued = True
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_pip_lb, pay_pip_lb = mp_proxy.build_pipette_item_action(
                                    lab_item_id
                                )
                                act_bld_lb, pay_bld_lb = mp_proxy.build_place_entity_action(
                                    site.lab_x, site.lab_y, direction=0
                                )
                                act_res, pay_res = mp_proxy.build_start_research_action(
                                    automation_tech_id
                                )
                                act_csp, pay_csp = mp_proxy.build_craft_action(
                                    science_recipe_id, count=10
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_pip_lb, pay_pip_lb),
                                    (act_bld_lb, pay_bld_lb),
                                    (act_clr, pay_clr),
                                    (act_res, pay_res),
                                    (act_csp, pay_csp),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                lab_place_and_science_queued
                                and not final_science_load_queued
                                and proxy.pending_action_count() == 0
                                and state.inventory.get(science_item_name, 0) >= 10
                            ):
                                final_science_load_queued = True
                                f4_x = 2.0 * site.furnace_x - site.furnace2_x
                                f4_y = 2.0 * site.furnace_y - site.furnace2_y
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_f1, pay_sel_f1 = mp_proxy.build_selected_entity_action(
                                    site.furnace_x, site.furnace_y
                                )
                                act_sel_f3, pay_sel_f3 = mp_proxy.build_selected_entity_action(
                                    site.furnace3_x, site.furnace3_y
                                )
                                act_sel_f4, pay_sel_f4 = mp_proxy.build_selected_entity_action(
                                    f4_x, f4_y
                                )
                                act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=False
                                )
                                act_sel_lb, pay_sel_lb = mp_proxy.build_selected_entity_action(
                                    site.lab_x, site.lab_y
                                )
                                act_sel_b, pay_sel_b = mp_proxy.build_selected_entity_action(
                                    site.boiler_x, site.boiler_y
                                )
                                act_pip_c, pay_pip_c = mp_proxy.build_pipette_item_action(
                                    fuel_item_id
                                )
                                act_pip_sp, pay_pip_sp = mp_proxy.build_pipette_item_action(
                                    science_item_id
                                )
                                act_ft_in, pay_ft_in = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=True
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_b, pay_sel_b),
                                    (act_pip_c, pay_pip_c),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                    (act_sel_lb, pay_sel_lb),
                                    (act_pip_sp, pay_pip_sp),
                                    (act_ft_in, pay_ft_in),
                                    (act_clr, pay_clr),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                final_science_load_queued
                                and not asm_craft_queued
                                and proxy.pending_action_count() == 0
                                and state.automation_researched
                            ):
                                asm_craft_queued = True
                                f4_x = 2.0 * site.furnace_x - site.furnace2_x
                                f4_y = 2.0 * site.furnace_y - site.furnace2_y
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_f1, pay_sel_f1 = mp_proxy.build_selected_entity_action(
                                    site.furnace_x, site.furnace_y
                                )
                                act_sel_f2, pay_sel_f2 = mp_proxy.build_selected_entity_action(
                                    site.furnace2_x, site.furnace2_y
                                )
                                act_sel_f3, pay_sel_f3 = mp_proxy.build_selected_entity_action(
                                    site.furnace3_x, site.furnace3_y
                                )
                                act_sel_f4, pay_sel_f4 = mp_proxy.build_selected_entity_action(
                                    f4_x, f4_y
                                )
                                act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=False
                                )
                                act_ca, pay_ca = mp_proxy.build_craft_action(asm_recipe_id, count=1)
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_ft_out, pay_ft_out),
                                    (act_ca, pay_ca),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                asm_craft_queued
                                and not asm_place_and_gears_queued
                                and proxy.pending_action_count() == 0
                                and state.inventory.get(target_item_name, 0) >= 1
                            ):
                                asm_place_and_gears_queued = True
                                phase1_gears_mc_target = state.machine_crafted_count + 9
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_pip_a, pay_pip_a = mp_proxy.build_pipette_item_action(
                                    target_item_id
                                )
                                act_bld_a, pay_bld_a = mp_proxy.build_place_entity_action(
                                    site.asm_x, site.asm_y, direction=0
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_pip_a, pay_pip_a),
                                    (act_bld_a, pay_bld_a),
                                    (act_clr, pay_clr),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                                for act, pay in encode_assembler_batch_actions(
                                    site.asm_x,
                                    site.asm_y,
                                    craft_recipe_name,
                                    (plate_item_name,),
                                    id_mappings,
                                    extract_first=False,
                                    quality_id=normal_quality_id,
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                asm_place_and_gears_queued
                                and not phase1_cables_queued
                                and proxy.pending_action_count() == 0
                                and state.machine_crafted_count >= phase1_gears_mc_target
                            ):
                                phase1_cables_queued = True
                                phase1_cables_mc_target = state.machine_crafted_count + 7
                                for act, pay in encode_assembler_batch_actions(
                                    site.asm_x,
                                    site.asm_y,
                                    cable_item_name,
                                    (copper_plate_name,),
                                    id_mappings,
                                    extract_first=True,
                                    quality_id=normal_quality_id,
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                phase1_cables_queued
                                and not phase1_circuits_queued
                                and proxy.pending_action_count() == 0
                                and state.machine_crafted_count >= phase1_cables_mc_target
                            ):
                                phase1_circuits_queued = True
                                phase1_circuits_mc_target = state.machine_crafted_count + 5
                                for act, pay in encode_assembler_batch_actions(
                                    site.asm_x,
                                    site.asm_y,
                                    circuit_item_name,
                                    (cable_item_name, plate_item_name),
                                    id_mappings,
                                    extract_first=True,
                                    quality_id=normal_quality_id,
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                phase1_circuits_queued
                                and not phase1_belts_queued
                                and proxy.pending_action_count() == 0
                                and state.machine_crafted_count >= phase1_circuits_mc_target
                            ):
                                phase1_belts_queued = True
                                phase1_belts_mc_target = state.machine_crafted_count + 2
                                for act, pay in encode_assembler_batch_actions(
                                    site.asm_x,
                                    site.asm_y,
                                    belt_item_name,
                                    (plate_item_name, craft_recipe_name),
                                    id_mappings,
                                    extract_first=True,
                                    quality_id=normal_quality_id,
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                phase1_belts_queued
                                and not phase1_inserters_queued
                                and proxy.pending_action_count() == 0
                                and state.machine_crafted_count >= phase1_belts_mc_target
                            ):
                                phase1_inserters_queued = True
                                phase1_inserters_mc_target = state.machine_crafted_count + 2
                                for act, pay in encode_assembler_batch_actions(
                                    site.asm_x,
                                    site.asm_y,
                                    inserter_item_name,
                                    (circuit_item_name, craft_recipe_name, plate_item_name),
                                    id_mappings,
                                    extract_first=True,
                                    quality_id=normal_quality_id,
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                phase1_inserters_queued
                                and not phase1_asm_queued
                                and proxy.pending_action_count() == 0
                                and state.machine_crafted_count >= phase1_inserters_mc_target
                            ):
                                phase1_asm_queued = True
                                phase1_asm_mc_target = state.machine_crafted_count + 1
                                phase1_asm_start_tick = state.tick
                                for act, pay in encode_assembler_batch_actions(
                                    site.asm_x,
                                    site.asm_y,
                                    target_item_name,
                                    (circuit_item_name, craft_recipe_name, plate_item_name),
                                    id_mappings,
                                    extract_first=True,
                                    quality_id=normal_quality_id,
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)
                            elif (
                                phase1_asm_queued
                                and not phase1_collected_queued
                                and proxy.pending_action_count() == 0
                                and state.machine_crafted_count >= phase1_asm_mc_target
                                and state.tick >= phase1_asm_start_tick + 75
                            ):
                                phase1_collected_queued = True
                                phase1_planned = True
                                f4_x = 2.0 * site.furnace_x - site.furnace2_x
                                f4_y = 2.0 * site.furnace_y - site.furnace2_y
                                act_clr, pay_clr = mp_proxy.build_clear_cursor_action()
                                act_sel_a, pay_sel_a = mp_proxy.build_selected_entity_action(
                                    site.asm_x, site.asm_y
                                )
                                act_sel_f1, pay_sel_f1 = mp_proxy.build_selected_entity_action(
                                    site.furnace_x, site.furnace_y
                                )
                                act_sel_f2, pay_sel_f2 = mp_proxy.build_selected_entity_action(
                                    site.furnace2_x, site.furnace2_y
                                )
                                act_sel_f3, pay_sel_f3 = mp_proxy.build_selected_entity_action(
                                    site.furnace3_x, site.furnace3_y
                                )
                                act_sel_f4, pay_sel_f4 = mp_proxy.build_selected_entity_action(
                                    f4_x, f4_y
                                )
                                act_ft_out, pay_ft_out = mp_proxy.build_fast_entity_transfer_action(
                                    into_entity=False
                                )
                                for act, pay in (
                                    (act_clr, pay_clr),
                                    (act_sel_a, pay_sel_a),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f1, pay_sel_f1),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f2, pay_sel_f2),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f4, pay_sel_f4),
                                    (act_ft_out, pay_ft_out),
                                    (act_sel_f3, pay_sel_f3),
                                    (act_ft_out, pay_ft_out),
                                ):
                                    proxy.queue_action(act, player_index=0, payload=pay)

                                phase1_target_task_indices = {t.task_index for t in pre_plan.tasks}
                            elif phase1_collected_queued and proxy.pending_action_count() == 0:
                                if (
                                    state.logistics_researched
                                    and state.inventory.get(belt_item_name, 0) >= 100
                                ):
                                    if not phase1_inventory_ready:
                                        phase1_inventory_ready = True
                                        proxy.max_actions_per_heartbeat = 12
                                if phase1_inventory_ready and bot_agent.current_task() is not None:
                                    bot_agent.step_lockstep_proxy(
                                        proxy,
                                        state,
                                        id_mappings,
                                        player_index=0,
                                        stop_tolerance=24.0,
                                    )

                    if (
                        mined_ore >= 148
                        and mined_resources[stone_item_name] >= 25
                        and mined_resources[fuel_item_name] >= 64
                        and mined_resources[copper_ore_name] >= 42
                        and last_state is not None
                        and last_state.hand_crafted_count == 106
                        and last_state.machine_crafted_count >= 28
                        and last_state.drills_active > 0
                        and last_state.plates_made > 0
                        and last_state.asm_made.get(craft_recipe_name, 0) > 0
                        and last_state.asm_made.get(belt_item_name, 0) > 0
                        and last_state.asm_made.get(science_item_name, 0) > 0
                        and last_state.steam_energy_generated > 0.0
                        and last_state.steam_power_researched
                        and last_state.electronics_researched
                        and last_state.science_pack_researched
                        and last_state.automation_researched
                        and last_state.logistics_researched
                        and final_science_load_queued
                        and phase1_collected_queued
                        and phase1_inventory_ready
                        and bot_agent.current_task() is None
                        and bot_agent.plan is not None
                        and len(
                            verify_placed_entities(
                                bot_agent.plan.layout.entities,
                                last_state.placed_entities,
                            )
                        )
                        == 0
                        and proxy.pending_action_count() == 0
                        and target_ach_id in proxy.stats.client_emitted_achievement_ids
                        and target_ach_id in proxy.stats.server_broadcast_achievement_ids
                        and research_ach_id in proxy.stats.client_emitted_achievement_ids
                        and research_ach_id in proxy.stats.server_broadcast_achievement_ids
                    ):
                        # Allow a brief pause so the achievement toast and final frames render
                        time.sleep(0.5)
                        success = True
                        break
                    time.sleep(0.01 if phase1_inventory_ready else 0.05)

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
                self.assertIsNotNone(
                    site,
                    f"Telemetry did not report a shoreline site.\n"
                    f"Last state: {last_state}\nServer log:\n{server_log}",
                )
                self.assertGreaterEqual(
                    mined_ore,
                    148,
                    f"Bot did not finish mining iron-ore patch ({res_name} at {res_x}, {res_y}).\n"
                    f"patch_idx={patch_idx}, walk_stage={walk_stage}, mining_phase={mining_phase}\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}\n"
                    f"Client log:\n{client_log}\nServer log:\n{server_log}",
                )
                self.assertGreaterEqual(
                    mined_resources[stone_item_name],
                    25,
                    f"Bot did not finish mining stone patch: {mined_resources}\n"
                    f"patch_idx={patch_idx}, walk_stage={walk_stage}, mining_phase={mining_phase}\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}\n"
                    f"Client log:\n{client_log[-2000:]}\nServer log:\n{server_log[-2000:]}",
                )
                self.assertGreaterEqual(
                    mined_resources[fuel_item_name],
                    64,
                    f"Bot did not finish mining coal patch: {mined_resources}",
                )
                self.assertGreaterEqual(
                    mined_resources[copper_ore_name],
                    42,
                    f"Bot did not finish mining copper-ore patch: {mined_resources}",
                )
                self.assertIsNotNone(last_state)
                assert last_state is not None
                self.assertEqual(
                    last_state.hand_crafted_count,
                    106,
                    f"Bot hand_crafted_count was not 106 (unseeded Phase 0 Lazy Bastard bootstrap).\n"
                    f"patch_idx={patch_idx}, shore_arrived={shore_arrived}, "
                    f"smelt_queued={smelt_queued}, power_craft={power_craft_queued}, "
                    f"power_placed={power_placed_queued}, lab_craft={lab_craft_queued}, "
                    f"lab_place_and_science={lab_place_and_science_queued}, "
                    f"final_science_load={final_science_load_queued}, "
                    f"asm_craft={asm_craft_queued}, asm_place_and_gears={asm_place_and_gears_queued}\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}",
                )
                self.assertGreaterEqual(
                    last_state.machine_crafted_count,
                    26,
                    f"Steam-powered assembling-machine-1s did not finish Phase 0 crafts.\n"
                    f"Stages: patch_idx={patch_idx}, walk_stage={walk_stage}, mining_phase={mining_phase}, "
                    f"shore_stage={shore_stage}, shore_arrived={shore_arrived}, "
                    f"smelt_queued={smelt_queued}, power_craft={power_craft_queued}, "
                    f"power_placed={power_placed_queued}, lab_craft={lab_craft_queued}, "
                    f"lab_place_and_science={lab_place_and_science_queued}, "
                    f"final_science_load={final_science_load_queued}, "
                    f"asm_craft={asm_craft_queued}, asm_place_and_gears={asm_place_and_gears_queued}, "
                    f"phase1_cables={phase1_cables_queued}, phase1_circuits={phase1_circuits_queued}, "
                    f"phase1_belts={phase1_belts_queued}, phase1_inserters={phase1_inserters_queued}, "
                    f"phase1_asm={phase1_asm_queued}, phase1_collected={phase1_collected_queued}, "
                    f"completed={len(bot_agent.completed_task_indices)}/{len(phase1_target_task_indices)}, "
                    f"current_task={bot_agent.current_task()}\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}\n"
                    f"Server log:\n{server_log[-3000:]}\nClient log:\n{client_log[-3000:]}",
                )
                self.assertGreaterEqual(
                    len(phase1_target_task_indices),
                    400,
                    f"Expected full synthesized starter base plan (>= 400 tasks), got {len(phase1_target_task_indices)}",
                )
                self.assertTrue(
                    phase1_planned
                    and phase1_collected_queued
                    and phase1_inventory_ready
                    and bot_agent.current_task() is None,
                    f"Phase 1 BotAgent starter base synthesis and UDP placement did not finish.\n"
                    f"phase1_planned={phase1_planned}, phase1_collected={phase1_collected_queued}, "
                    f"phase1_inventory_ready={phase1_inventory_ready}, "
                    f"completed={len(bot_agent.completed_task_indices)}/{len(phase1_target_task_indices)}, "
                    f"current_task={bot_agent.current_task()}\n"
                    f"Last state: {last_state}",
                )
                self.assertIsNotNone(bot_agent.plan)
                assert bot_agent.plan is not None
                placement_errors = verify_placed_entities(
                    bot_agent.plan.layout.entities,
                    last_state.placed_entities,
                )
                self.assertEqual(
                    placement_errors,
                    [],
                    f"Surface entity verification failed ({len(placement_errors)} errors): {placement_errors[:20]}",
                )
                self.assertGreaterEqual(
                    last_state.machine_crafted_count,
                    28,
                    f"Starter base assemblers did not finish crafting: {last_state}",
                )
                self.assertGreater(
                    last_state.drills_active,
                    0,
                    f"Synthesized electric-mining-drills are not active.\nLast state: {last_state}",
                )
                self.assertGreater(
                    last_state.plates_made,
                    0,
                    f"Synthesized starter base stone-furnaces did not smelt plates.\nLast state: {last_state}",
                )
                self.assertGreater(
                    last_state.asm_made.get(craft_recipe_name, 0),
                    0,
                    f"Synthesized starter base gear assemblers did not craft {craft_recipe_name}.\n"
                    f"asm_made={last_state.asm_made}",
                )
                self.assertGreater(
                    last_state.asm_made.get(belt_item_name, 0),
                    0,
                    f"Synthesized starter base mall assembler did not craft {belt_item_name}.\n"
                    f"asm_made={last_state.asm_made}",
                )
                self.assertGreater(
                    last_state.asm_made.get(science_item_name, 0),
                    0,
                    f"Synthesized starter base science assembler did not craft {science_item_name}.\n"
                    f"asm_made={last_state.asm_made}",
                )
                self.assertGreater(
                    last_state.steam_energy_generated,
                    0.0,
                    f"Steam-engine did not generate electricity on the lakeside electric network.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}",
                )
                self.assertTrue(
                    last_state.steam_power_researched,
                    f"Iron-plate smelting did not trigger steam-power technology.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}",
                )
                self.assertTrue(
                    last_state.electronics_researched,
                    f"Copper-plate smelting did not trigger electronics technology.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}",
                )
                self.assertTrue(
                    last_state.science_pack_researched,
                    f"Crafting lab did not trigger automation-science-pack technology.\n"
                    f"Last state: {last_state}\nStats: {proxy.stats}",
                )
                self.assertTrue(
                    last_state.automation_researched,
                    f"Powered lab did not finish researching automation technology.\n"
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
                    f"Client did not complete lakeside Lazy Bastard expansion and unlock "
                    f"{target_ach_name}={target_ach_id} and {research_ach_name}={research_ach_id} "
                    f"after finding/mining {res_name}, walking to shoreline {site}, smelting, "
                    f"machine-crafting furnaces/gears/belts/cables/circuits/lab/science, and "
                    f"researching {automation_tech_name} in a steam-powered lab.\n"
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
