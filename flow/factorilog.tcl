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
if {![info exists BLOCK_FILL]} {
    set BLOCK_FILL 0.08
}
if {![info exists CELL_PAD_SITES]} {
    set CELL_PAD_SITES 4
}
if {![info exists CORE_MARGIN]} {
    set CORE_MARGIN 4
}
if {![info exists density]} {
    set density 0.18
}

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

# Non-default spacing rule for fluid nets to prevent adjacent surface pipe clashes:
create_ndr -name fluid_ndr -spacing 1.25
set fluid_count 0
foreach net [$block getNets] {
    set nname [$net getName]
    if {[string match "*oil*" $nname] || [string match "*water*" $nname] || [string match "*petroleum*" $nname] || [string match "*acid*" $nname] || [string match "*lubricant*" $nname] || [string match "*steam*" $nname]} {
        assign_ndr -ndr fluid_ndr -net $nname
        incr fluid_count
    }
}
if {$fluid_count > 0} {
    puts "factorilog: assigned non-default spacing rule (1.25) to $fluid_count fluid nets"
}

# --- io -------------------------------------------------------------
# Place pins on the die edge: inputs on the left (West), outputs on the right (East).
set_io_pin_constraint -direction input -region left:*
set_io_pin_constraint -direction output -region right:*
place_pins -hor_layers M1 -ver_layers M2

# --- place ----------------------------------------------------------
# The placer target is its own uniform density floor: the tightest
# packing the pad allows. The space left over from BLOCK_FILL stays
# free as the belt channel. Global placement spreads the blocks,
# legalization snaps them to tile rows, and the check proves every
# block sits on the grid with no overlap and no site misalignment.
puts "factorilog: placement density [format %.3f $density]"
if {![info exists SPLIT_PAD_SITES]} {
    set SPLIT_PAD_SITES 5
}
set split_pad [expr {max($CELL_PAD_SITES, $SPLIT_PAD_SITES)}]
set_placement_padding -global -left $CELL_PAD_SITES -right $CELL_PAD_SITES
set_placement_padding -masters {belt_merge2 belt_split2 belt_balancer2} -left $split_pad -right $split_pad

if {[info exists ROUTABILITY_DRIVEN] && $ROUTABILITY_DRIVEN == "1"} {
    global_placement -density $density \
        -pad_left $CELL_PAD_SITES \
        -pad_right $CELL_PAD_SITES \
        -routability_driven
} else {
    global_placement -density $density \
        -pad_left $CELL_PAD_SITES \
        -pad_right $CELL_PAD_SITES
}

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

# --- pin untangling -------------------------------------------------
# Untangle symmetric splitter outputs and merger inputs based on placed coordinates.
proc get_net_sink_center_y {net dbu} {
    set sum_y 0.0
    set count 0
    foreach iterm [$net getITerms] {
        if {[$iterm getIoType] == "INPUT"} {
            set inst [$iterm getInst]
            lassign [$inst getLocation] ix iy
            set h [expr {[[$inst getMaster] getHeight] / double($dbu)}]
            set cent_y [expr {$iy / double($dbu) + $h / 2.0}]
            set sum_y [expr {$sum_y + $cent_y}]
            incr count
        }
    }
    foreach bterm [$net getBTerms] {
        if {[$bterm getIoType] == "OUTPUT"} {
            foreach bpin [$bterm getBPins] {
                set box [$bpin getBox]
                set cent_y [expr {([$box yMin] + [$box yMax]) / 2.0 / double($dbu)}]
                set sum_y [expr {$sum_y + $cent_y}]
                incr count
            }
        }
    }
    if {$count > 0} {
        return [expr {$sum_y / double($count)}]
    }
    return 0.0
}

proc get_net_driver_center_y {net dbu} {
    set sum_y 0.0
    set count 0
    foreach iterm [$net getITerms] {
        if {[$iterm getIoType] == "OUTPUT"} {
            set inst [$iterm getInst]
            lassign [$inst getLocation] ix iy
            set h [expr {[[$inst getMaster] getHeight] / double($dbu)}]
            set cent_y [expr {$iy / double($dbu) + $h / 2.0}]
            set sum_y [expr {$sum_y + $cent_y}]
            incr count
        }
    }
    foreach bterm [$net getBTerms] {
        if {[$bterm getIoType] == "INPUT"} {
            foreach bpin [$bterm getBPins] {
                set box [$bpin getBox]
                set cent_y [expr {([$box yMin] + [$box yMax]) / 2.0 / double($dbu)}]
                set sum_y [expr {$sum_y + $cent_y}]
                incr count
            }
        }
    }
    if {$count > 0} {
        return [expr {$sum_y / double($count)}]
    }
    return 0.0
}

proc do_untangle {block dbu} {
    for {set pass 0} {$pass < 2} {incr pass} {
        foreach inst [$block getInsts] {
            set mname [[$inst getMaster] getName]
            if {[string match "*split2" $mname]} {
                set it1 [$inst findITerm "y1"]
                set it2 [$inst findITerm "y2"]
                if {$it1 != "NULL" && $it2 != "NULL"} {
                    set n1 [$it1 getNet]
                    set n2 [$it2 getNet]
                    if {$n1 != "NULL" && $n2 != "NULL" && $n1 != $n2} {
                        set y1 [get_net_sink_center_y $n1 $dbu]
                        set y2 [get_net_sink_center_y $n2 $dbu]
                        if {$y1 > $y2} {
                            odb::dbITerm_disconnect $it1
                            odb::dbITerm_disconnect $it2
                            odb::dbITerm_connect $it1 $n2
                            odb::dbITerm_connect $it2 $n1
                        }
                    }
                }
            }
            if {[string match "*merge2" $mname]} {
                set ita [$inst findITerm "a"]
                set itb [$inst findITerm "b"]
                if {$ita != "NULL" && $itb != "NULL"} {
                    set na [$ita getNet]
                    set nb [$itb getNet]
                    if {$na != "NULL" && $nb != "NULL" && $na != $nb} {
                        set ya [get_net_driver_center_y $na $dbu]
                        set yb [get_net_driver_center_y $nb $dbu]
                        lassign [$inst getLocation] ix_dbu iy_dbu
                        set my [expr {$iy_dbu / double($dbu) + 1.0}]
                        if {$ya > $my && $yb > $my} {
                            if {$ya < $yb} {
                                odb::dbITerm_disconnect $ita
                                odb::dbITerm_disconnect $itb
                                odb::dbITerm_connect $ita $nb
                                odb::dbITerm_connect $itb $na
                            }
                        } else {
                            if {$ya > $yb} {
                                odb::dbITerm_disconnect $ita
                                odb::dbITerm_disconnect $itb
                                odb::dbITerm_connect $ita $nb
                                odb::dbITerm_connect $itb $na
                            }
                        }
                    }
                }
            }
        }
    }
}

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
    set_routing_layers -signal M1-M2
}
::drt::detailed_route_set_unidirectional_layer M1
::drt::detailed_route_set_unidirectional_layer M2
::drt::detailed_route_set_unidirectional_layer M3
::drt::detailed_route_set_unidirectional_layer M4

do_untangle $block $dbu

if {[info exists LAYER_ADJUSTMENT] && $LAYER_ADJUSTMENT != ""} {
    set_global_routing_layer_adjustment M1 $LAYER_ADJUSTMENT
    set_global_routing_layer_adjustment M2 $LAYER_ADJUSTMENT
}
global_route -verbose

detailed_route -output_drc route_drc.rpt -verbose 1

write_def factory.def
puts "factorilog: routed DEF written"

# --- timing ---------------------------------------------------------
# Static timing analysis across factory combinational paths.
create_clock -name virtual_clk -period 1000
set_input_delay -clock virtual_clk 0 [all_inputs]
set_output_delay -clock virtual_clk 0 [all_outputs]
report_checks -path_delay max -fields {input_pins slew capacitance} -digits 4 > timing.rpt
report_worst_slack -max >> timing.rpt
