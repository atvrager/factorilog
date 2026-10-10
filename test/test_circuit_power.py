"""Unit tests for circuit clocks, dual-color networks, and power switches.

This test suite verifies:
1. PDK definitions for power switch and combinator macros.
2. Two-color accumulator clocks with red and green circuit wires.
3. Three-stage combinator ring oscillators with alternating wires.
4. Two-phase alternating power domains controlled by dual power switches.
5. Headless Factorio physical simulation and blueprint validation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import sys

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "foundry"))
sys.path.insert(0, str(REPO_ROOT / "flow"))
sys.path.insert(0, str(REPO_ROOT / "test"))

import blueprint
import check_blueprint
import circuit
import circuit_synth
import lef
import liberty
import pdk
import solve
from test_routing_primitives import run_factorio_simulation



class TestCircuitPower(unittest.TestCase):
    """Test circuit networks, multi-color clocks, and power switches."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        if not cls.fixture_path.is_file():
            raise unittest.SkipTest("Fixture dump-fixture.json not found.")

        try:
            cls.game_bin = check_blueprint.find_factorio_binary()
        except FileNotFoundError:
            raise unittest.SkipTest("Factorio binary not found.")

        cls.data = solve.Data(str(cls.fixture_path))
        cls.pdk = pdk.Pdk(cls.data)

    def test_pdk_power_switch_and_combinator_macros(self) -> None:
        """Verify power switch and combinator macro definitions in LEF and Liberty."""
        switch_cell = pdk.power_switch_cell()
        self.assertEqual(switch_cell["cell"], pdk.POWER_SWITCH_CELL)
        self.assertEqual(switch_cell["machine"], "power-switch")
        self.assertEqual(switch_cell["width"], 2)
        self.assertEqual(switch_cell["height"], 2)
        self.assertTrue(switch_cell.get("circuit_cell"))

        cells = [
            switch_cell,
            pdk.display_panel_cell(),
            pdk.arithmetic_combinator_cell(),
            pdk.decider_combinator_cell(),
            pdk.constant_combinator_cell(),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            lef_path = Path(tmp) / "tech.lef"
            lib_path = Path(tmp) / "tech.lib"
            lef_text = lef.write_lef(self.pdk, cells, str(lef_path))
            lib_text = liberty.write_liberty(cells, str(lib_path))

            self.assertIn("MACRO power_switch", lef_text)
            self.assertIn("MACRO arithmetic_combinator", lef_text)
            self.assertIn("MACRO decider_combinator", lef_text)
            self.assertIn("MACRO constant_combinator", lef_text)

            lef_errs = lef.check_lef(cells, lef_text)
            self.assertEqual(lef_errs, [])

            lib_errs = liberty.check_liberty(cells, lib_text)
            self.assertEqual(lib_errs, [])

    def test_two_color_accumulator_clock_power_switch(self) -> None:
        """Verify two-color clock driving power switch in Factorio simulation."""
        init_lua = """
        local ee = surface.create_entity{name = "electric-energy-interface", position = {-8, 0}, force = "player"}
        local pole_gen = surface.create_entity{name = "medium-electric-pole", position = {-8, 0}, force = "player"}
        local pole_main = surface.create_entity{name = "medium-electric-pole", position = {-5, 0}, force = "player"}
        local ps = surface.create_entity{name = "power-switch", position = {-2, 0}, force = "player"}
        local pole_down = surface.create_entity{name = "medium-electric-pole", position = {2, 0}, force = "player"}
        local radar = surface.create_entity{name = "radar", position = {3, 0}, force = "player"}

        -- Disconnect automatic copper wires
        local up_conn = pole_main.get_wire_connector(defines.wire_connector_id.pole_copper, true)
        up_conn.disconnect_all()
        local down_conn = pole_down.get_wire_connector(defines.wire_connector_id.pole_copper, true)
        down_conn.disconnect_all()
        local ps_left = ps.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true)
        ps_left.disconnect_all()
        local ps_right = ps.get_wire_connector(defines.wire_connector_id.power_switch_right_copper, true)
        ps_right.disconnect_all()

        -- Connect power from generator to main pole
        pole_gen.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(up_conn, false)

        -- Connect power through switch: Main to Left, Right to Downstream
        up_conn.connect_to(ps_left, false)
        ps_right.connect_to(down_conn, false)

        -- Clock combinators near main pole
        local decid = surface.create_entity{name = "decider-combinator", position = {-5, 2}, direction = 0, force = "player"}
        local const = surface.create_entity{name = "constant-combinator", position = {-4, 2}, direction = 0, force = "player"}

        local dc_cb = decid.get_or_create_control_behavior()
        dc_cb.parameters = {
            conditions = {
                {
                    first_signal = {type = "virtual", name = "signal-T"},
                    comparator = "<",
                    constant = 60
                }
            },
            outputs = {
                {
                    signal = {type = "virtual", name = "signal-T"},
                    copy_count_from_input = true
                }
            }
        }

        local cc_cb = const.get_or_create_control_behavior()
        local s1 = cc_cb.get_section(1) or cc_cb.add_section()
        s1.set_slot(1, {value = "signal-T", min = 1})

        -- Decider RED wire feedback
        local d_in_r = decid.get_wire_connector(defines.wire_connector_id.combinator_input_red, true)
        local d_out_r = decid.get_wire_connector(defines.wire_connector_id.combinator_output_red, true)
        d_out_r.connect_to(d_in_r, false)

        -- Constant GREEN wire to Decider input
        local c_out_g = const.get_wire_connector(defines.wire_connector_id.circuit_green, true)
        local d_in_g = decid.get_wire_connector(defines.wire_connector_id.combinator_input_green, true)
        c_out_g.connect_to(d_in_g, false)

        -- Wire Decider output (RED) to Power Switch circuit (RED)
        local ps_red = ps.get_wire_connector(defines.wire_connector_id.circuit_red, true)
        d_out_r.connect_to(ps_red, false)

        -- Configure Power Switch control condition: T < 30
        local ps_cb = ps.get_or_create_control_behavior()
        ps_cb.circuit_enable_disable = true
        ps_cb.circuit_condition = {
            first_signal = {type = "virtual", name = "signal-T"},
            comparator = "<",
            constant = 30
        }

        storage.ps = ps
        storage.radar = radar
        storage.decid = decid
        """

        tick_lua = """
        if event.tick % 10 == 0 and event.tick <= 120 then
            local t_val = storage.decid.get_signal({type = "virtual", name = "signal-T"}, defines.wire_connector_id.combinator_output_red)
            local sw = storage.ps.power_switch_state
            local energy = storage.radar.energy
            log("TEST_RESULT: tick=" .. event.tick .. " T=" .. t_val .. " switch=" .. tostring(sw) .. " energy=" .. tostring(energy))
        end
        """

        results = run_factorio_simulation(init_lua, tick_lua, ticks=130)
        self.assertGreater(len(results), 0)

        data_points: dict[int, tuple[int, bool, float]] = {}
        for line in results:
            if "TEST_RESULT:" not in line:
                continue
            parts = line.split("TEST_RESULT:")[1].strip().split()
            kv = dict(p.split("=") for p in parts)
            tick = int(kv["tick"])
            t_val = int(kv["T"])
            sw = kv["switch"] == "true"
            energy = float(kv["energy"])
            data_points[tick] = (t_val, sw, energy)

        # Verify active phase: ticks 10, 20, 30 have switch=True and energy > 0
        for t in (10, 20, 30):
            self.assertIn(t, data_points)
            t_val, sw, energy = data_points[t]
            self.assertTrue(sw, f"Expected switch open at tick {t}")
            self.assertGreater(energy, 0.0, f"Expected positive energy at tick {t}")

        # Verify inactive phase: ticks 40, 50, 60 have switch=False and energy == 0
        for t in (40, 50, 60):
            self.assertIn(t, data_points)
            t_val, sw, energy = data_points[t]
            self.assertFalse(sw, f"Expected switch closed at tick {t}")
            self.assertEqual(energy, 0.0, f"Expected zero energy at tick {t}")

        # Verify cycle repeats at tick 70, 80, 90
        for t in (70, 80, 90):
            self.assertIn(t, data_points)
            t_val, sw, energy = data_points[t]
            self.assertTrue(sw, f"Expected switch open at tick {t}")
            self.assertGreater(energy, 0.0, f"Expected positive energy at tick {t}")

    def test_three_stage_ring_oscillator_clock(self) -> None:
        """Verify three-stage combinator ring oscillator with alternating wires."""
        init_lua = """
        local ee = surface.create_entity{name = "electric-energy-interface", position = {2, -2}, force = "player"}
        local pole = surface.create_entity{name = "medium-electric-pole", position = {2, -1}, force = "player"}

        local c1 = surface.create_entity{name = "arithmetic-combinator", position = {1, 1}, direction = 0, force = "player"}
        local c2 = surface.create_entity{name = "arithmetic-combinator", position = {2, 1}, direction = 0, force = "player"}
        local c3 = surface.create_entity{name = "arithmetic-combinator", position = {3, 1}, direction = 0, force = "player"}
        local ps = surface.create_entity{name = "power-switch", position = {0, 3}, force = "player"}

        -- C1: 1 - A -> A (inverter)
        local cb1 = c1.get_or_create_control_behavior()
        cb1.parameters = {
            first_constant = 1,
            operation = "-",
            second_signal = {type = "virtual", name = "signal-A"},
            output_signal = {type = "virtual", name = "signal-A"}
        }

        -- C2: A + 0 -> A (buffer)
        local cb2 = c2.get_or_create_control_behavior()
        cb2.parameters = {
            first_signal = {type = "virtual", name = "signal-A"},
            operation = "+",
            second_constant = 0,
            output_signal = {type = "virtual", name = "signal-A"}
        }

        -- C3: A + 0 -> A (buffer)
        local cb3 = c3.get_or_create_control_behavior()
        cb3.parameters = {
            first_signal = {type = "virtual", name = "signal-A"},
            operation = "+",
            second_constant = 0,
            output_signal = {type = "virtual", name = "signal-A"}
        }

        -- C1 out -> C2 in (RED wire)
        local c1_out_r = c1.get_wire_connector(defines.wire_connector_id.combinator_output_red, true)
        local c2_in_r = c2.get_wire_connector(defines.wire_connector_id.combinator_input_red, true)
        c1_out_r.connect_to(c2_in_r, false)

        -- C2 out -> C3 in (GREEN wire)
        local c2_out_g = c2.get_wire_connector(defines.wire_connector_id.combinator_output_green, true)
        local c3_in_g = c3.get_wire_connector(defines.wire_connector_id.combinator_input_green, true)
        c2_out_g.connect_to(c3_in_g, false)

        -- C3 out -> C1 in (RED wire)
        local c3_out_r = c3.get_wire_connector(defines.wire_connector_id.combinator_output_red, true)
        local c1_in_r = c1.get_wire_connector(defines.wire_connector_id.combinator_input_red, true)
        c3_out_r.connect_to(c1_in_r, false)

        -- C1 out -> Power Switch circuit (RED wire)
        local ps_red = ps.get_wire_connector(defines.wire_connector_id.circuit_red, true)
        c1_out_r.connect_to(ps_red, false)

        local ps_cb = ps.get_or_create_control_behavior()
        ps_cb.circuit_enable_disable = true
        ps_cb.circuit_condition = {
            first_signal = {type = "virtual", name = "signal-A"},
            comparator = ">",
            constant = 0
        }

        storage.c1 = c1
        storage.c2 = c2
        storage.c3 = c3
        storage.ps = ps
        """

        tick_lua = """
        if event.tick >= 2 and event.tick <= 14 then
            local v1 = storage.c1.get_signal({type = "virtual", name = "signal-A"}, defines.wire_connector_id.combinator_output_red)
            local sw = storage.ps.power_switch_state
            log("TEST_RESULT: tick=" .. event.tick .. " v1=" .. v1 .. " sw=" .. tostring(sw))
        end
        """

        results = run_factorio_simulation(init_lua, tick_lua, ticks=20)
        self.assertGreater(len(results), 0)

        data: dict[int, tuple[int, bool]] = {}
        for line in results:
            if "TEST_RESULT:" not in line:
                continue
            parts = line.split("TEST_RESULT:")[1].strip().split()
            kv = dict(p.split("=") for p in parts)
            data[int(kv["tick"])] = (int(kv["v1"]), kv["sw"] == "true")

        # Verify 3-tick HIGH oscillator phase (ticks 2, 3, 4)
        for t in (2, 3, 4):
            self.assertEqual(data[t][0], 1, f"Expected signal-A=1 at tick {t}")

        # Verify 3-tick LOW oscillator phase (ticks 5, 6, 7)
        for t in (5, 6, 7):
            self.assertEqual(data[t][0], 0, f"Expected signal-A=0 at tick {t}")

        # Verify second cycle 3-tick HIGH oscillator phase (ticks 8, 9, 10)
        for t in (8, 9, 10):
            self.assertEqual(data[t][0], 1, f"Expected signal-A=1 at tick {t}")

        # Verify power switch operates with 1-tick latency relative to signal-A
        for t in (3, 4, 5):
            self.assertTrue(data[t][1], f"Expected power switch active at tick {t}")
        for t in (6, 7, 8):
            self.assertFalse(data[t][1], f"Expected power switch inactive at tick {t}")
        for t in (9, 10, 11):
            self.assertTrue(data[t][1], f"Expected power switch active at tick {t}")
        for t in (12, 13, 14):
            self.assertFalse(data[t][1], f"Expected power switch inactive at tick {t}")

    def test_two_phase_alternating_power_domains(self) -> None:
        """Verify dual power switches operating complementary power domains."""
        init_lua = """
        local ee = surface.create_entity{name = "electric-energy-interface", position = {-8, 0}, force = "player"}
        local pole_gen = surface.create_entity{name = "medium-electric-pole", position = {-8, 0}, force = "player"}
        local pole_main = surface.create_entity{name = "medium-electric-pole", position = {-5, 0}, force = "player"}

        local ps_a = surface.create_entity{name = "power-switch", position = {-3, -3}, force = "player"}
        local ps_b = surface.create_entity{name = "power-switch", position = {-3, 3}, force = "player"}

        local pole_a = surface.create_entity{name = "medium-electric-pole", position = {0, -3}, force = "player"}
        local radar_a = surface.create_entity{name = "radar", position = {2, -3}, force = "player"}

        local pole_b = surface.create_entity{name = "medium-electric-pole", position = {0, 3}, force = "player"}
        local radar_b = surface.create_entity{name = "radar", position = {2, 3}, force = "player"}

        -- Disconnect all automatic copper wires
        pole_main.get_wire_connector(defines.wire_connector_id.pole_copper, true).disconnect_all()
        pole_a.get_wire_connector(defines.wire_connector_id.pole_copper, true).disconnect_all()
        pole_b.get_wire_connector(defines.wire_connector_id.pole_copper, true).disconnect_all()
        ps_a.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true).disconnect_all()
        ps_a.get_wire_connector(defines.wire_connector_id.power_switch_right_copper, true).disconnect_all()
        ps_b.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true).disconnect_all()
        ps_b.get_wire_connector(defines.wire_connector_id.power_switch_right_copper, true).disconnect_all()

        -- Connect power from generator to main pole
        pole_gen.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(
            pole_main.get_wire_connector(defines.wire_connector_id.pole_copper, true), false)

        -- Connect Main to ps_a left, ps_a right to pole_a
        pole_main.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(
            ps_a.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true), false)
        ps_a.get_wire_connector(defines.wire_connector_id.power_switch_right_copper, true).connect_to(
            pole_a.get_wire_connector(defines.wire_connector_id.pole_copper, true), false)

        -- Connect Main to ps_b left, ps_b right to pole_b
        pole_main.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(
            ps_b.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true), false)
        ps_b.get_wire_connector(defines.wire_connector_id.power_switch_right_copper, true).connect_to(
            pole_b.get_wire_connector(defines.wire_connector_id.pole_copper, true), false)

        -- Clock near pole_main
        local decid_clk = surface.create_entity{name = "decider-combinator", position = {-5, 1}, direction = 0, force = "player"}
        local const_clk = surface.create_entity{name = "constant-combinator", position = {-4, 1}, direction = 0, force = "player"}

        local dc_cb = decid_clk.get_or_create_control_behavior()
        dc_cb.parameters = {
            conditions = {
                {
                    first_signal = {type = "virtual", name = "signal-T"},
                    comparator = "<",
                    constant = 60
                }
            },
            outputs = {
                {
                    signal = {type = "virtual", name = "signal-T"},
                    copy_count_from_input = true
                }
            }
        }

        local cc_cb = const_clk.get_or_create_control_behavior()
        local s1 = cc_cb.get_section(1) or cc_cb.add_section()
        s1.set_slot(1, {value = "signal-T", min = 1})

        -- Decider RED feedback
        local clk_in_r = decid_clk.get_wire_connector(defines.wire_connector_id.combinator_input_red, true)
        local clk_out_r = decid_clk.get_wire_connector(defines.wire_connector_id.combinator_output_red, true)
        clk_out_r.connect_to(clk_in_r, false)

        -- Constant GREEN to Decider input
        local const_out_g = const_clk.get_wire_connector(defines.wire_connector_id.circuit_green, true)
        local clk_in_g = decid_clk.get_wire_connector(defines.wire_connector_id.combinator_input_green, true)
        const_out_g.connect_to(clk_in_g, false)

        -- Wire Decider output (RED) to PS_A circuit RED
        local ps_a_red = ps_a.get_wire_connector(defines.wire_connector_id.circuit_red, true)
        clk_out_r.connect_to(ps_a_red, false)

        -- Wire Decider output (GREEN) to PS_B circuit GREEN
        local clk_out_g = decid_clk.get_wire_connector(defines.wire_connector_id.combinator_output_green, true)
        local ps_b_green = ps_b.get_wire_connector(defines.wire_connector_id.circuit_green, true)
        clk_out_g.connect_to(ps_b_green, false)

        -- PS_A condition: T < 30 on RED
        local cb_a = ps_a.get_or_create_control_behavior()
        cb_a.circuit_enable_disable = true
        cb_a.circuit_condition = {
            first_signal = {type = "virtual", name = "signal-T"},
            comparator = "<",
            constant = 30
        }

        -- PS_B condition: T > 29 on GREEN
        local cb_b = ps_b.get_or_create_control_behavior()
        cb_b.circuit_enable_disable = true
        cb_b.circuit_condition = {
            first_signal = {type = "virtual", name = "signal-T"},
            comparator = ">",
            constant = 29
        }

        storage.ps_a = ps_a
        storage.ps_b = ps_b
        storage.radar_a = radar_a
        storage.radar_b = radar_b
        storage.decid = decid_clk
        """

        tick_lua = """
        if event.tick % 10 == 0 and event.tick <= 90 then
            local sw_a = storage.ps_a.power_switch_state
            local sw_b = storage.ps_b.power_switch_state
            local ea = storage.radar_a.energy
            local eb = storage.radar_b.energy
            log("TEST_RESULT: tick=" .. event.tick .. " swA=" .. tostring(sw_a) .. " swB=" .. tostring(sw_b) .. " ea=" .. ea .. " eb=" .. eb)
        end
        """

        results = run_factorio_simulation(init_lua, tick_lua, ticks=100)
        self.assertGreater(len(results), 0)

        data: dict[int, tuple[bool, bool, float, float]] = {}
        for line in results:
            if "TEST_RESULT:" not in line:
                continue
            parts = line.split("TEST_RESULT:")[1].strip().split()
            kv = dict(p.split("=") for p in parts)
            tick = int(kv["tick"])
            data[tick] = (
                kv["swA"] == "true",
                kv["swB"] == "true",
                float(kv["ea"]),
                float(kv["eb"]),
            )

        # Domain A active, Domain B inactive (ticks 10, 20, 30)
        for t in (10, 20, 30):
            sw_a, sw_b, ea, eb = data[t]
            self.assertTrue(sw_a, f"Expected Switch A true at tick {t}")
            self.assertFalse(sw_b, f"Expected Switch B false at tick {t}")
            self.assertGreater(ea, 0.0, f"Expected Domain A energy at tick {t}")
            self.assertEqual(eb, 0.0, f"Expected Domain B zero energy at tick {t}")

        # Domain A inactive, Domain B active (ticks 40, 50, 60)
        for t in (40, 50, 60):
            sw_a, sw_b, ea, eb = data[t]
            self.assertFalse(sw_a, f"Expected Switch A false at tick {t}")
            self.assertTrue(sw_b, f"Expected Switch B true at tick {t}")
            self.assertEqual(ea, 0.0, f"Expected Domain A zero energy at tick {t}")
            self.assertGreater(eb, 0.0, f"Expected Domain B energy at tick {t}")

        # Mutual exclusivity check across all reported ticks
        for t, (sw_a, sw_b, _ea, _eb) in data.items():
            self.assertFalse(
                sw_a and sw_b,
                f"Violation of mutual exclusivity: both switches active at tick {t}",
            )

    def test_power_switch_and_clock_blueprint_roundtrip(self) -> None:
        """Verify blueprint encoding and headless Factorio placement of power switch and clock."""
        entities = [
            {
                "entity_number": 1,
                "name": "medium-electric-pole",
                "position": {"x": -2.5, "y": 0.5},
            },
            {
                "entity_number": 2,
                "name": "power-switch",
                "position": {"x": 0.0, "y": 0.0},
                "control_behavior": {
                    "circuit_enabled": True,
                    "circuit_condition": {
                        "first_signal": {"type": "virtual", "name": "signal-T"},
                        "comparator": "<",
                        "constant": 30,
                    },
                },
                "switch_state": False,
            },
            {
                "entity_number": 3,
                "name": "medium-electric-pole",
                "position": {"x": 2.5, "y": 0.5},
            },
            {
                "entity_number": 4,
                "name": "decider-combinator",
                "position": {"x": -2.5, "y": 2.5},
                "direction": 0,
                "control_behavior": {
                    "decider_conditions": {
                        "conditions": [
                            {
                                "first_signal": {"type": "virtual", "name": "signal-T"},
                                "comparator": "<",
                                "constant": 60,
                            }
                        ],
                        "outputs": [
                            {
                                "signal": {"type": "virtual", "name": "signal-T"},
                                "copy_count_from_input": True,
                            }
                        ],
                    }
                },
            },
            {
                "entity_number": 5,
                "name": "constant-combinator",
                "position": {"x": -1.5, "y": 2.5},
                "control_behavior": {
                    "sections": {
                        "sections": [
                            {
                                "index": 1,
                                "filters": [
                                    {
                                        "index": 1,
                                        "type": "virtual",
                                        "name": "signal-T",
                                        "quality": "normal",
                                        "comparator": "=",
                                        "count": 1,
                                    }
                                ],
                            }
                        ]
                    }
                },
            },
        ]

        wires = [
            [1, 5, 2, 5],
            [2, 6, 3, 5],
            [4, 3, 4, 1],
            [5, 2, 4, 2],
            [4, 3, 2, 1],
        ]

        bp_str = blueprint.encode(entities, "test_power_switch_clock", wires=wires)
        self.assertTrue(bp_str.startswith("0"))

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="test-bp-circ-") as tmp:
            results = check_blueprint.validate_blueprint(bp_str, self.game_bin, Path(tmp))
            self.assertEqual(results.get("RESULT_UNPLACED_TOTAL"), 0)
            self.assertEqual(results.get("RESULT_MATCHED"), results.get("RESULT_TOTAL"))
            self.assertEqual(results.get("COUNT_power-switch"), 1)
            self.assertEqual(results.get("COUNT_decider-combinator"), 1)
            self.assertEqual(results.get("COUNT_constant-combinator"), 1)
            self.assertEqual(results.get("COUNT_medium-electric-pole"), 2)

    def test_synthesized_verilog_circuit_wires_in_simulation(self) -> None:
        """Verify in-game wire connections on synthesized Verilog combinators and switch."""
        v_path = REPO_ROOT / "flow" / "controllers" / "power_sequencer.v"
        synth_result = circuit_synth.synthesize_verilog(v_path)

        entities: list[dict[str, Any]] = []
        inst_to_entity: dict[str, dict[str, Any]] = {}
        grid_entities: dict[tuple[float, float], dict[str, Any]] = {}

        # Place combinators and switch in a compact test grid (within 9 tiles reach)
        for idx, c in enumerate(synth_result.cells):
            ename = (
                "constant-combinator"
                if "constant" in c.cell
                else (
                    "power-switch"
                    if "switch" in c.cell
                    else ("arithmetic-combinator" if "arithmetic" in c.cell else "decider-combinator")
                )
            )
            extra: dict[str, Any] = {}
            if c.control_behavior:
                extra["control_behavior"] = c.control_behavior
            ent = {
                "name": ename,
                "position": {"x": 2.0 * (idx % 5), "y": 2.0 * (idx // 5)},
                "direction": 0,
                **extra,
            }
            entities.append(ent)
            inst_to_entity[c.name] = ent
            grid_entities[(round(ent["position"]["x"], 2), round(ent["position"]["y"], 2))] = ent

        # Add two power poles
        pole1 = {"name": "medium-electric-pole", "position": {"x": 0.0, "y": 6.0}}
        pole2 = {"name": "medium-electric-pole", "position": {"x": 4.0, "y": 6.0}}
        entities.extend([pole1, pole2])

        pole_entities = {
            (0.0, 6.0): pole1,
            (4.0, 6.0): pole2,
        }
        mst_connections = [((0.0, 6.0), (4.0, 6.0))]

        netlist_data = {
            "instances": [
                {"name": c.name, "cell": c.cell, "control_behavior": c.control_behavior}
                for c in synth_result.cells
            ],
            "circuit_connections": [
                {
                    "source": c.source,
                    "target": c.target,
                    "color": c.color,
                    "source_port": c.source_port,
                    "target_port": c.target_port,
                }
                for c in synth_result.connections
            ],
            "telemetry": [],
        }

        wires = circuit.wire_telemetry(
            entities,
            netlist_data,
            inst_to_entity,
            {},
            grid_entities,
            mst_connections,
            pole_entities,
        )

        # Python-level assertions: wires must include combinators, switches, and poles
        comb_wires = [
            w for w in wires if any("combinator" in entities[num - 1]["name"] for num in (w[0], w[2]))
        ]
        switch_wires = [
            w for w in wires if any("switch" in entities[num - 1]["name"] for num in (w[0], w[2]))
        ]
        self.assertGreater(len(comb_wires), 5, "Combinators must have multiple wires in wires array")
        self.assertEqual(len(switch_wires), 3, "Power switch must have 1 circuit and 2 copper wires")

        bp_str = blueprint.encode(entities, "test_synth_circuit", wires=wires)

        init_lua = f"""
        local BP = [==[{bp_str}]==]
        game.forces.player.research_all_technologies()
        local surface = game.surfaces[1]
        for cx = -2, 2 do
            for cy = -2, 2 do
                surface.request_to_generate_chunks({{cx * 32, cy * 32}}, 1)
            end
        end
        surface.force_generate_chunk_requests()

        local tiles = {{}}
        for x = -10, 20 do
            for y = -10, 20 do
                tiles[#tiles + 1] = {{name = "concrete", position = {{x, y}}}}
            end
        end
        surface.set_tiles(tiles)

        for _, entity in pairs(surface.find_entities_filtered{{area = {{{{-10, -10}}, {{20, 20}}}}}}) do
            if entity.name ~= "character" then
                entity.destroy()
            end
        end

        local inv = game.create_inventory(1)
        local stack = inv[1]
        stack.set_stack{{name = "blueprint", count = 1}}
        local import_res = stack.import_stack(BP)

        local built = stack.build_blueprint{{
            surface = surface,
            force = game.forces.player,
            position = {{0, 0}},
            force_build = true
        }}

        local ghosts = surface.find_entities_filtered{{name = "entity-ghost"}}
        for _, g in ipairs(ghosts) do
            g.silent_revive()
        end
        """

        tick_lua = """
        local surface = game.surfaces[1]
        local decs = surface.find_entities_filtered{name = "decider-combinator"}
        local switches = surface.find_entities_filtered{name = "power-switch"}
        local consts = surface.find_entities_filtered{name = "constant-combinator"}

        local connected_decs = 0
        for _, d in ipairs(decs) do
            local in_r = d.get_wire_connector(defines.wire_connector_id.combinator_input_red, true)
            local in_g = d.get_wire_connector(defines.wire_connector_id.combinator_input_green, true)
            local out_r = d.get_wire_connector(defines.wire_connector_id.combinator_output_red, true)
            local out_g = d.get_wire_connector(defines.wire_connector_id.combinator_output_green, true)
            local total_conns = #in_r.connections + #in_g.connections + #out_r.connections + #out_g.connections
            if total_conns > 0 then
                connected_decs = connected_decs + 1
            end
        end

        local ps = switches[1]
        local switch_wire_count = 0
        if ps then
            local c_r = ps.get_wire_connector(defines.wire_connector_id.circuit_red, true)
            local c_left = ps.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true)
            local c_right = ps.get_wire_connector(defines.wire_connector_id.power_switch_right_copper, true)
            switch_wire_count = #c_r.connections + #c_left.connections + #c_right.connections
        end

        local const_wire_count = 0
        local cc = consts[1]
        if cc then
            local out_r = cc.get_wire_connector(defines.wire_connector_id.circuit_red, true)
            const_wire_count = #out_r.connections
        end

        log("TEST_RESULT: decs=" .. #decs .. " conn_decs=" .. connected_decs .. " switch_wires=" .. switch_wire_count .. " const_wires=" .. const_wire_count)
        """

        results = run_factorio_simulation(init_lua, tick_lua, ticks=2)
        self.assertGreater(len(results), 0, "Simulation results must not be empty")

        matched = False
        for line in results:
            if "TEST_RESULT: decs=" in line:
                matched = True
                self.assertIn("decs=7 conn_decs=7", line)
                self.assertIn("switch_wires=3", line)
                self.assertIn("const_wires=5", line)

        self.assertTrue(matched, f"Test result pattern not found in simulation output: {results}")


if __name__ == "__main__":
    unittest.main()

