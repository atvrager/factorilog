# factorilog.tcl - the OpenROAD stage of the Factorio PDK flow.
#
# Reads the generated PDK (tech.lef, tech.lib) and the netlist, then
# runs the physical stages. One tile is one LEF micron, so every
# coordinate below is a tile on the Factorio ground grid.
#
#   1. floorplan   die and core area, rows of site tile, routing tracks
#   2. io          the raw inputs and the science outputs on the edge
#   3. place       global placement, legalization, placement check
#
# usage: flow/orfs.sh            # from the repo root, writes factory.def

# --- PDK target parameters ------------------------------------------
# BLOCK_FILL is the share of the ground the buildings cover. The rest
# is the belt channel. A block needs its neighbours apart, because two
# adjacent buildings do not move items: an inserter or a belt tile
# does. CELL_PAD_SITES sets that gap.
#
# The pad raises the effective block footprint by 70 percent for a 3x3
# block, so a high fill leaves the legalizer no padded slot to move a
# block into. Measured on the 1220 block fixture, 1 tile of pad:
#   fill 28 percent: legalizer settles in 5 passes, wirelength +66
#   fill 35 percent: 147 passes, wirelength +287
# Above 35 percent it does not converge at all.
set BLOCK_FILL 0.08
set CELL_PAD_SITES 4
set CORE_MARGIN 4

read_liberty tech.lib
read_lef tech.lef
read_verilog netlist.v
link_design factory

set_thread_count [exec nproc]

set block [ord::get_db_block]
set dbu [[ord::get_db] getDbuPerMicron]

# --- floorplan ------------------------------------------------------
# The die holds the block area at the block fill, plus the margin. The
# pad does not enter this sum: the pad is the belt channel between the
# blocks, and it comes out of the free ground. Largest block is 5x5
# tiles (oil refinery), so the core spans many blocks both ways.
set block_area 0
foreach inst [$block getInsts] {
    set master [$inst getMaster]
    set w [expr {[$master getWidth] / $dbu}]
    set h [expr {[$master getHeight] / $dbu}]
    set block_area [expr {$block_area + $w * $h}]
}

set core_side [expr {int(ceil(sqrt($block_area / $BLOCK_FILL)))}]
set die_side [expr {$core_side + 2 * $CORE_MARGIN}]
set core_lo $CORE_MARGIN
set core_hi [expr {$die_side - $CORE_MARGIN}]
set core_area [expr {($core_hi - $core_lo) * ($core_hi - $core_lo)}]
puts "factorilog: [llength [$block getInsts]] instances, $block_area block tiles,\
 die $die_side x $die_side tiles, fill [format %.1f \
[expr {100.0 * $block_area / $core_area}]] percent"

initialize_floorplan -die_area "0 0 $die_side $die_side" \
    -core_area "$core_lo $core_lo $core_hi $core_hi" \
    -site tile

puts "factorilog: [llength [$block getRows]] rows"

# Routing tracks come from the LEF pitches: one track per tile on each
# layer. M1 and M2 carry surface tracks, M3 and M4 carry underground tunnels.
make_tracks

# --- place ----------------------------------------------------------
# The placer target is its own uniform density floor: the tightest
# packing the pad allows. The space left over from BLOCK_FILL stays
# free as the belt channel. Global placement spreads the blocks,
# legalization snaps them to tile rows, and the check proves every
# block sits on the grid with no overlap and no site misalignment.
# That check raises an error on any violation, and the error is fatal
# here.
#
# The pad reaches legalization through set_placement_padding. The pad
# argument of the placer only shapes its density model. Pad one tile
# each side of every block, so two neighbouring columns of blocks end
# up two tiles apart: one belt lane and one inserter lane, which is
# the smallest game-legal connection between two buildings. Rows stack
# without a gap, as they do in the game.
#
# CTS and STA do not run. The Liberty timing tables are stubs, so the
# design has no clock and no delay to repair.
# --- io -------------------------------------------------------------
# Place pins on the die edge closest to their connected instances.
place_pins -hor_layers M1 -ver_layers M2

set density 0.18
puts "factorilog: placement density [format %.3f $density]"
global_placement -density $density \
    -pad_left $CELL_PAD_SITES \
    -pad_right $CELL_PAD_SITES

set_placement_padding -global -left $CELL_PAD_SITES -right $CELL_PAD_SITES
set_placement_padding -masters {belt_merge2 belt_split2 belt_balancer2} -left 5 -right 5
place_pins -hor_layers M1 -ver_layers M2
detailed_placement

if {[catch {check_placement} problem]} {
    error "factorilog: placement check failed: $problem"
}

report_design_area

set placed 0
foreach inst [$block getInsts] {
    if {[$inst getPlacementStatus] == "PLACED"} {
        incr placed
    }
}
puts "factorilog: $placed of [llength [$block getInsts]] placed on the tile grid"

# --- route ----------------------------------------------------------
# The router connects blocks across four layers:
# M1 and M2 for surface tracks, M3 and M4 for underground tunnels.
# FastRoute runs global routing. TritonRoute runs detailed routing.
set has_circuit 0
foreach inst [$block getInsts] {
    set master_name [[$inst getMaster] getName]
    if {[string match "*combinator*" $master_name] || [string match "*display*" $master_name] || [string match "*switch*" $master_name]} {
        set has_circuit 1
        break
    }
}
if {$has_circuit} {
    set_routing_layers -signal M1-M6
} else {
    set_routing_layers -signal M1-M4
}
::drt::detailed_route_set_unidirectional_layer M1
::drt::detailed_route_set_unidirectional_layer M2
::drt::detailed_route_set_unidirectional_layer M3
::drt::detailed_route_set_unidirectional_layer M4
global_route -verbose
detailed_route -output_drc route_drc.rpt -verbose 1

write_def factory.def
puts "factorilog: routed DEF written"

# --- timing ---------------------------------------------------------
# Static timing analysis across factory combinational paths.
create_clock -name virtual_clk -period 1000
set_input_delay -clock virtual_clk 0 [all_inputs]
set_output_delay -clock virtual_clk 0 [all_outputs]
report_checks -path_delay max -fields {input_pins slew capacitance} -digits 4
report_worst_slack -max
