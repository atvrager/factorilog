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
if {![info exists SPLIT_PAD_SITES]} {
    set SPLIT_PAD_SITES 10
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

# Check for ranks.tcl sidecar (Structured Linear Row Placement)
set has_ranks 0
if {[file exists "ranks.tcl"]} {
    source "ranks.tcl"
    if {[info exists num_ranks] && $num_ranks > 0} {
        set has_ranks 1
    }
}

if {[info exists USE_ROW_PLACEMENT] && $USE_ROW_PLACEMENT == "1" && $has_ranks} {
    # Structured Linear Row Placement:
    # Arrange machines in horizontal rows by recipe topological depth.
    set pad_between [expr {max(8, $CELL_PAD_SITES)}]
    set channel_h 12
    if {[info exists CHANNEL_H] && $CHANNEL_H != ""} {
        set channel_h $CHANNEL_H
    }

    # Use rank_offset_x computed algorithmically in netlist.py and sourced from ranks.tcl.

    set max_row_w 0
    for {set r 0} {$r < $num_ranks} {incr r} {
        set roff 0
        if {[info exists rank_offset_x($r)]} { set roff $rank_offset_x($r) }
        set row_w $roff
        set rank_max_h 3
        if {[info exists rank_instances($r)]} {
            foreach iname $rank_instances($r) {
                set dinst [$block findInst $iname]
                if {$dinst != "NULL" && $dinst != ""} {
                    set dmaster [$dinst getMaster]
                    set dw [expr {[$dmaster getWidth] / $dbu}]
                    set dh [expr {[$dmaster getHeight] / $dbu}]
                    set pad $pad_between
                    if {[string match "*merge*" $iname]} {
                        set pad 16
                    } elseif {[string match "*split*" $iname] || [string match "*bal*" $iname]} {
                        set pad [expr {max(10, $SPLIT_PAD_SITES)}]
                    } elseif {[string match "*disp*" $iname] || [string match "*acc*" $iname] || [string match "*comb*" $iname] || [string match "*dec*" $iname] || [string match "*latch*" $iname] || [string match "*switch*" $iname] || [string match "*params*" $iname]} {
                        set pad 2
                    }
                    set row_w [expr {$row_w + $dw + $pad}]
                    if {$dh > $rank_max_h} {
                        set rank_max_h $dh
                    }
                }
            }
        }
        set rank_h($r) $rank_max_h
        if {$row_w > $max_row_w} {
            set max_row_w $row_w
        }
    }

    set core_w [expr {max(30, $max_row_w + 4)}]
    set total_h $channel_h
    for {set r 0} {$r < $num_ranks} {incr r} {
        set row_start_y($r) [expr {$CORE_MARGIN + $total_h}]
        set total_h [expr {$total_h + $rank_h($r) + $channel_h}]
    }
    set core_h $total_h
    set die_w [expr {$core_w + 2 * $CORE_MARGIN}]
    set die_h [expr {$core_h + 2 * $CORE_MARGIN}]
    set core_lo_x $CORE_MARGIN
    set core_lo_y $CORE_MARGIN
    set core_hi_x [expr {$die_w - $CORE_MARGIN}]
    set core_hi_y [expr {$die_h - $CORE_MARGIN}]

    puts "factorilog: structured linear row floorplan ($num_ranks ranks): die ${die_w}x${die_h}, core ${core_w}x${core_h}"

    initialize_floorplan -die_area "0 0 $die_w $die_h" \
        -core_area "$core_lo_x $core_lo_y $core_hi_x $core_hi_y" \
        -site tile

    make_tracks

    set dbTech [[$block getDataBase] getTech]
    set m2Layer [$dbTech findLayer "M2"]

    # Assign positions to instances in each rank row
    for {set r 0} {$r < $num_ranks} {incr r} {
        set ry $row_start_y($r)
        set roff 0
        if {[info exists rank_offset_x($r)]} { set roff $rank_offset_x($r) }
        set rx [expr {$core_lo_x + 2 + $roff}]
        if {[info exists rank_instances($r)]} {
            foreach iname $rank_instances($r) {
                set dinst [$block findInst $iname]
                if {$dinst != "NULL" && $dinst != ""} {
                    set dmaster [$dinst getMaster]
                    set dw [expr {[$dmaster getWidth] / $dbu}]
                    set dh [expr {[$dmaster getHeight] / $dbu}]
                    $dinst setLocation [expr {int($rx * $dbu)}] [expr {int($ry * $dbu)}]
                    $dinst setPlacementStatus PLACED

                    set pad $pad_between
                    if {[string match "*merge*" $iname]} {
                        set pad 16
                    } elseif {[string match "*split*" $iname] || [string match "*bal*" $iname]} {
                        set pad [expr {max(10, $SPLIT_PAD_SITES)}]
                    } elseif {[string match "*disp*" $iname] || [string match "*acc*" $iname] || [string match "*comb*" $iname] || [string match "*dec*" $iname] || [string match "*latch*" $iname] || [string match "*switch*" $iname] || [string match "*params*" $iname]} {
                        set pad 2
                    }
                    set rx [expr {$rx + $dw + $pad}]
                }
            }
        }
    }

    # Place any unranked instances at top of core
    set unranked_x [expr {$core_lo_x + 2}]
    set unranked_y [expr {$core_hi_y - 4}]
    foreach inst [$block getInsts] {
        if {[$inst getPlacementStatus] != "PLACED"} {
            set master [$inst getMaster]
            set dw [expr {[$master getWidth] / $dbu}]
            $inst setLocation [expr {int($unranked_x * $dbu)}] [expr {int($unranked_y * $dbu)}]
            $inst setPlacementStatus PLACED
            set unranked_x [expr {$unranked_x + $dw + $pad_between}]
        }
    }

    # Place pins on the die edge: inputs on left, outputs on right
    set_io_pin_constraint -direction input -region left:*
    set_io_pin_constraint -direction output -region right:*
    place_pins -hor_layers M1 -ver_layers M2

    detailed_placement
    if {[catch {check_placement} problem]} {
        error "factorilog: placement check failed: $problem"
    }

} else {
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

    make_tracks

    # Place pins on the die edge: inputs on the left (West), outputs on the right (East).
    set_io_pin_constraint -direction input -region left:*
    set_io_pin_constraint -direction output -region right:*
    place_pins -hor_layers M1 -ver_layers M2

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
}

# Non-default spacing rule for fluid nets to prevent adjacent surface pipe clashes:
create_ndr -name fluid_ndr -spacing 1.25
set fluid_count 0
if {[info exists fluid_nets]} {
    foreach fn $fluid_nets {
        set net [$block findNet $fn]
        if {$net != "NULL"} {
            assign_ndr -ndr fluid_ndr -net $fn
            incr fluid_count
        }
    }
}
if {$fluid_count > 0} {
    puts "factorilog: assigned non-default spacing rule (1.25) to $fluid_count fluid nets"
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
        if {[[$iterm getMTerm] getIoType] == "INPUT"} {
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
                set box [$bpin getBBox]
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

proc get_net_sink_center_x {net dbu} {
    set sum_x 0.0
    set count 0
    foreach iterm [$net getITerms] {
        if {[[$iterm getMTerm] getIoType] == "INPUT"} {
            set inst [$iterm getInst]
            lassign [$inst getLocation] ix iy
            set w [expr {[[$inst getMaster] getWidth] / double($dbu)}]
            set cent_x [expr {$ix / double($dbu) + $w / 2.0}]
            set sum_x [expr {$sum_x + $cent_x}]
            incr count
        }
    }
    foreach bterm [$net getBTerms] {
        if {[$bterm getIoType] == "OUTPUT"} {
            foreach bpin [$bterm getBPins] {
                set box [$bpin getBBox]
                set cent_x [expr {([$box xMin] + [$box xMax]) / 2.0 / double($dbu)}]
                set sum_x [expr {$sum_x + $cent_x}]
                incr count
            }
        }
    }
    if {$count > 0} {
        return [expr {$sum_x / double($count)}]
    }
    return 0.0
}

proc get_net_driver_center_y {net dbu} {
    set sum_y 0.0
    set count 0
    foreach iterm [$net getITerms] {
        if {[[$iterm getMTerm] getIoType] == "OUTPUT"} {
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
                set box [$bpin getBBox]
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

proc get_net_driver_center_x {net dbu} {
    set sum_x 0.0
    set count 0
    foreach iterm [$net getITerms] {
        if {[[$iterm getMTerm] getIoType] == "OUTPUT"} {
            set inst [$iterm getInst]
            lassign [$inst getLocation] ix iy
            set w [expr {[[$inst getMaster] getWidth] / double($dbu)}]
            set cent_x [expr {$ix / double($dbu) + $w / 2.0}]
            set sum_x [expr {$sum_x + $cent_x}]
            incr count
        }
    }
    foreach bterm [$net getBTerms] {
        if {[$bterm getIoType] == "INPUT"} {
            foreach bpin [$bterm getBPins] {
                set box [$bpin getBBox]
                set cent_x [expr {([$box xMin] + [$box xMax]) / 2.0 / double($dbu)}]
                set sum_x [expr {$sum_x + $cent_x}]
                incr count
            }
        }
    }
    if {$count > 0} {
        return [expr {$sum_x / double($count)}]
    }
    return 0.0
}


proc do_untangle {block dbu} {
    for {set pass 0} {$pass < 2} {incr pass} {
        foreach inst [$block getInsts] {
            set mname [[$inst getMaster] getName]
            if {[string match "*split2" $mname] || [string match "*balancer2" $mname]} {
                set it1 [$inst findITerm "y1"]
                set it2 [$inst findITerm "y2"]
                if {$it1 != "NULL" && $it2 != "NULL"} {
                    set n1 [$it1 getNet]
                    set n2 [$it2 getNet]
                    if {$n1 != "NULL" && $n2 != "NULL" && $n1 != $n2} {
                        lassign [$inst getLocation] sx_dbu sy_dbu
                        set sy [expr {$sy_dbu / double($dbu)}]
                        set y1 [get_net_sink_center_y $n1 $dbu]
                        set y2 [get_net_sink_center_y $n2 $dbu]
                        if {[expr {abs($y1 - $y2)}] >= 0.5} {
                            if {$y1 > $y2} {
                                odb::dbITerm_disconnect $it1
                                odb::dbITerm_disconnect $it2
                                odb::dbITerm_connect $it1 $n2
                                odb::dbITerm_connect $it2 $n1
                            }
                        } else {
                            set x1 [get_net_sink_center_x $n1 $dbu]
                            set x2 [get_net_sink_center_x $n2 $dbu]
                            if {$x1 > $x2} {
                                odb::dbITerm_disconnect $it1
                                odb::dbITerm_disconnect $it2
                                odb::dbITerm_connect $it1 $n2
                                odb::dbITerm_connect $it2 $n1
                            }
                        }
                    }
                }
            }
            if {[string match "*split2" $mname]} {
                set ita [$inst findITerm "a"]
                set itb [$inst findITerm "b"]
                if {$ita != "NULL" && $itb != "NULL"} {
                    set na [$ita getNet]
                    set nb [$itb getNet]
                    lassign [$inst getLocation] sx_dbu sy_dbu
                    set sy [expr {$sy_dbu / double($dbu)}]
                    if {$na != "NULL" && ($nb == "NULL" || $nb == "")} {
                        set y_drv [get_net_driver_center_y $na $dbu]
                        if {[expr {abs($y_drv - ($sy + 1.5))}] < [expr {abs($y_drv - ($sy + 0.5))}]} {
                            odb::dbITerm_disconnect $ita
                            odb::dbITerm_connect $itb $na
                        }
                    } elseif {$nb != "NULL" && ($na == "NULL" || $na == "")} {
                        set y_drv [get_net_driver_center_y $nb $dbu]
                        if {[expr {abs($y_drv - ($sy + 0.5))}] < [expr {abs($y_drv - ($sy + 1.5))}]} {
                            odb::dbITerm_disconnect $itb
                            odb::dbITerm_connect $ita $nb
                        }
                    }
                }
            }
            if {[string match "*merge2" $mname] || [string match "*balancer2" $mname]} {
                set ita [$inst findITerm "a"]
                if {$ita == "NULL"} { set ita [$inst findITerm "a1"] }
                set itb [$inst findITerm "b"]
                if {$itb == "NULL"} { set itb [$inst findITerm "a2"] }
                if {$ita != "NULL" && $itb != "NULL"} {
                    set na [$ita getNet]
                    set nb [$itb getNet]
                    if {$na != "NULL" && $nb != "NULL" && $na != $nb} {
                        set ya [get_net_driver_center_y $na $dbu]
                        set yb [get_net_driver_center_y $nb $dbu]
                        lassign [$inst getLocation] ix_dbu iy_dbu
                        set mx [expr {$ix_dbu / double($dbu)}]
                        if {[expr {abs($ya - $yb)}] < 0.5} {
                            set xa [get_net_driver_center_x $na $dbu]
                            set xb [get_net_driver_center_x $nb $dbu]
                            if {$xa > $xb} {
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
set tech [ord::get_db_tech]
foreach layer [$tech getLayers] {
    if {[$layer getType] == "ROUTING"} {
        set lname [$layer getName]
        ::drt::detailed_route_set_unidirectional_layer $lname
    }
}
set_routing_layers -signal M1-M2

if {[info exists DO_UNTANGLE] && $DO_UNTANGLE == "1"} {
    puts "factorilog: running do_untangle"
    do_untangle $block $dbu
}

if {[info exists LAYER_ADJUSTMENT] && $LAYER_ADJUSTMENT != ""} {
    set_global_routing_layer_adjustment M1 $LAYER_ADJUSTMENT
    set_global_routing_layer_adjustment M2 $LAYER_ADJUSTMENT
}
global_route -verbose

set droute_iters 20
if {[info exists DROUTE_END_ITER]} {
    set droute_iters $DROUTE_END_ITER
}
detailed_route -droute_end_iter $droute_iters -output_drc route_drc.rpt -verbose 1

set unrouted_count 0
foreach net [$block getNets] {
    if {[$net getWire] == "NULL" || [$net getWire] == ""} {
        incr unrouted_count
        set it_info ""
        foreach it [$net getITerms] {
            set mterm [$it getMTerm]
            lassign [[$it getInst] getLocation] ix iy
            append it_info " ([[$mterm getMaster] getName]/[$mterm getName] at [expr {$ix/$dbu}],[expr {$iy/$dbu}])"
        }
        puts "factorilog: unrouted net [$net getName]$it_info"
    }
}
puts "factorilog: total unrouted nets: $unrouted_count"

write_def factory.def
puts "factorilog: routed DEF written"

# --- timing ---------------------------------------------------------
# Static timing analysis across factory combinational paths.
create_clock -name virtual_clk -period 1000
set_input_delay -clock virtual_clk 0 [all_inputs]
set_output_delay -clock virtual_clk 0 [all_outputs]
report_checks -path_delay max -fields {input_pins slew capacitance} -digits 4 > timing.rpt
report_worst_slack -max >> timing.rpt
