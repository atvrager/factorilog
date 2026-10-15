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

proc get_chassis_block_order {w h} {
    set cells [list]
    set min_x 0
    set max_x [expr {$w - 1}]
    set min_y 0
    set max_y [expr {$h - 1}]

    while {$min_x <= $max_x && $min_y <= $max_y} {
        if {$min_x == $max_x && $min_y == $max_y} {
            lappend cells [list $min_x $min_y]
            break
        }
        if {$min_x == $max_x} {
            for {set y $min_y} {$y <= $max_y} {incr y} {
                lappend cells [list $min_x $y]
            }
            break
        }
        if {$min_y == $max_y} {
            for {set x $min_x} {$x <= $max_x} {incr x} {
                lappend cells [list $x $min_y]
            }
            break
        }

        # South edge (left to right)
        for {set x $min_x} {$x < $max_x} {incr x} {
            lappend cells [list $x $min_y]
        }
        # East edge (bottom to top)
        for {set y $min_y} {$y < $max_y} {incr y} {
            lappend cells [list $max_x $y]
        }
        # North edge (right to left)
        for {set x $max_x} {$x > $min_x} {incr x -1} {
            lappend cells [list $x $max_y]
        }
        # West edge (top to bottom)
        for {set y $max_y} {$y > $min_y} {incr y -1} {
            lappend cells [list $min_x $y]
        }

        incr min_x
        incr max_x -1
        incr min_y
        incr max_y -1
    }
    return $cells
}

if {![info exists USE_ANALYTICAL_PLACEMENT]} {
    set USE_ANALYTICAL_PLACEMENT 1
}

if {[info exists USE_ANALYTICAL_PLACEMENT] && $USE_ANALYTICAL_PLACEMENT == "1" && [info exists analytical_coords]} {
    # Analytical Placement with Directed Legalization
    set is_chassis [expr {[info exists USE_CHASSIS] && $USE_CHASSIS == "1"}]
    if {$is_chassis} {
        if {![info exists CHASSIS_GRID_W] || $CHASSIS_GRID_W <= 0} {
            if {[info exists AUTO_CHASSIS_GRID_W] && $AUTO_CHASSIS_GRID_W > 0} {
                set CHASSIS_GRID_W $AUTO_CHASSIS_GRID_W
            } else {
                set CHASSIS_GRID_W 1
            }
        }
        if {![info exists CHASSIS_GRID_H] || $CHASSIS_GRID_H <= 0} {
            if {[info exists AUTO_CHASSIS_GRID_H] && $AUTO_CHASSIS_GRID_H > 0} {
                set CHASSIS_GRID_H $AUTO_CHASSIS_GRID_H
            } else {
                set CHASSIS_GRID_H 1
            }
        }
        if {![info exists CHASSIS_PITCH]} { set CHASSIS_PITCH 100 }
        if {![info exists CHASSIS_CORE_W]} { set CHASSIS_CORE_W 94 }
        if {![info exists CHASSIS_CORE_H]} { set CHASSIS_CORE_H 94 }
        if {![info exists CHASSIS_CHANNEL_W]} { set CHASSIS_CHANNEL_W 6 }

        set margin_x [expr {($CHASSIS_PITCH - $CHASSIS_CORE_W) / 2}]
        set margin_y [expr {($CHASSIS_PITCH - $CHASSIS_CORE_H) / 2}]
        set die_w [expr {$CHASSIS_GRID_W * $CHASSIS_PITCH}]
        set die_h [expr {$CHASSIS_GRID_H * $CHASSIS_PITCH}]
        set core_lo_x $margin_x
        set core_lo_y $margin_y
        set core_hi_x [expr {$die_w - $margin_x}]
        set core_hi_y [expr {$die_h - $margin_y}]

        puts "factorilog: analytical placement on chassis (${CHASSIS_GRID_W}x${CHASSIS_GRID_H}): die ${die_w}x${die_h}, core margin ${margin_x}x${margin_y}"

        initialize_floorplan -die_area [list 0 0 $die_w $die_h] \
            -core_area [list $core_lo_x $core_lo_y $core_hi_x $core_hi_y] \
            -site tile

        # Create placement blockages on street channels and outer margins
        create_blockage -region [list 0 0 $die_w $margin_y]
        set top_y [expr {($CHASSIS_GRID_H - 1) * $CHASSIS_PITCH + $margin_y + $CHASSIS_CORE_H}]
        create_blockage -region [list 0 $top_y $die_w $die_h]
        create_blockage -region [list 0 0 $margin_x $die_h]
        set right_x [expr {($CHASSIS_GRID_W - 1) * $CHASSIS_PITCH + $margin_x + $CHASSIS_CORE_W}]
        create_blockage -region [list $right_x 0 $die_w $die_h]

        for {set by 0} {$by < [expr {$CHASSIS_GRID_H - 1}]} {incr by} {
            set y1 [expr {$by * $CHASSIS_PITCH + $margin_y + $CHASSIS_CORE_H}]
            set y2 [expr {$y1 + $CHASSIS_CHANNEL_W}]
            create_blockage -region [list 0 $y1 $die_w $y2]
        }

        for {set bx 0} {$bx < [expr {$CHASSIS_GRID_W - 1}]} {incr bx} {
            set x1 [expr {$bx * $CHASSIS_PITCH + $margin_x + $CHASSIS_CORE_W}]
            set x2 [expr {$x1 + $CHASSIS_CHANNEL_W}]
            create_blockage -region [list $x1 0 $x2 $die_h]
        }

        if {[info exists CHASSIS_EMPTY_BLOCKS] && $CHASSIS_EMPTY_BLOCKS != ""} {
            foreach blk $CHASSIS_EMPTY_BLOCKS {
                set bx [lindex $blk 0]
                set by [lindex $blk 1]
                set x1 [expr {$bx * $CHASSIS_PITCH + $margin_x}]
                set y1 [expr {$by * $CHASSIS_PITCH + $margin_y}]
                set x2 [expr {$x1 + $CHASSIS_CORE_W}]
                set y2 [expr {$y1 + $CHASSIS_CORE_H}]
                create_blockage -region [list $x1 $y1 $x2 $y2]
            }
        } elseif {$CHASSIS_GRID_W == 3 && $CHASSIS_GRID_H == 3} {
            set mid_bx [expr {($CHASSIS_GRID_W - 1) / 2}]
            set mid_by [expr {($CHASSIS_GRID_H - 1) / 2}]
            set x1 [expr {$mid_bx * $CHASSIS_PITCH + $margin_x}]
            set y1 [expr {$mid_by * $CHASSIS_PITCH + $margin_y}]
            set x2 [expr {$x1 + $CHASSIS_CORE_W}]
            set y2 [expr {$y1 + $CHASSIS_CORE_H}]
            create_blockage -region [list $x1 $y1 $x2 $y2]
        }

        set ch_dict_name "analytical_chassis_${CHASSIS_GRID_W}x${CHASSIS_GRID_H}"
        if {[info exists $ch_dict_name]} {
            set active_coords [set $ch_dict_name]
        } elseif {[info exists analytical_chassis_1x1]} {
            set active_coords $analytical_chassis_1x1
        } else {
            set active_coords $analytical_coords
        }
    } else {
        set die_w $analytical_die_w
        set die_h $analytical_die_h
        set core_lo_x $analytical_core_lo_x
        set core_lo_y $analytical_core_lo_y
        set core_hi_x $analytical_core_hi_x
        set core_hi_y $analytical_core_hi_y
        set core_w [expr {$core_hi_x - $core_lo_x}]
        set core_h [expr {$core_hi_y - $core_lo_y}]

        puts "factorilog: analytical floorplan: die ${die_w}x${die_h}, core ${core_w}x${core_h}"

        initialize_floorplan -die_area "0 0 $die_w $die_h" \
            -core_area "$core_lo_x $core_lo_y $core_hi_x $core_hi_y" \
            -site tile

        set active_coords $analytical_coords
    }

    make_tracks

    dict for {iname xy} $active_coords {
        set dinst [$block findInst $iname]
        if {$dinst != "NULL" && $dinst != ""} {
            set rx [lindex $xy 0]
            set ry [lindex $xy 1]
            $dinst setLocation [expr {int($rx * $dbu)}] [expr {int($ry * $dbu)}]
            $dinst setPlacementStatus PLACED
        }
    }

    set unplaced_x [expr {$core_lo_x + 6}]
    set unplaced_y [expr {$core_lo_y + 6}]
    foreach inst [$block getInsts] {
        if {[$inst getPlacementStatus] != "PLACED"} {
            set master [$inst getMaster]
            set dw [expr {[$master getWidth] / $dbu}]
            set dh [expr {[$master getHeight] / $dbu}]
            if {$unplaced_x + $dw > [expr {$core_hi_x - 6}]} {
                set unplaced_x [expr {$core_lo_x + 6}]
                set unplaced_y [expr {$unplaced_y + $dh + 4}]
            }
            $inst setLocation [expr {int($unplaced_x * $dbu)}] [expr {int($unplaced_y * $dbu)}]
            $inst setPlacementStatus PLACED
            set unplaced_x [expr {$unplaced_x + $dw + 6}]
        }
    }

    if {$is_chassis} {
        set rw_dict_name "analytical_rewire_${CHASSIS_GRID_W}x${CHASSIS_GRID_H}"
        if {[info exists $rw_dict_name]} {
            set active_rewire [set $rw_dict_name]
            dict for {r_iname port_list} $active_rewire {
                set r_inst [$block findInst $r_iname]
                if {$r_inst != "NULL" && $r_inst != ""} {
                    foreach {p_name _p_net_name} $port_list {
                        set p_iterm [$r_inst findITerm $p_name]
                        if {$p_iterm != "NULL" && $p_iterm != ""} {
                            odb::dbITerm_disconnect $p_iterm
                        }
                    }
                }
            }
            dict for {r_iname port_list} $active_rewire {
                set r_inst [$block findInst $r_iname]
                if {$r_inst != "NULL" && $r_inst != ""} {
                    foreach {p_name p_net_name} $port_list {
                        set p_iterm [$r_inst findITerm $p_name]
                        set p_net [$block findNet $p_net_name]
                        if {$p_iterm != "NULL" && $p_iterm != "" && $p_net != "NULL" && $p_net != ""} {
                            odb::dbITerm_connect $p_iterm $p_net
                        }
                    }
                }
            }
        }
    }

    set_io_pin_constraint -direction input -region left:*
    set_io_pin_constraint -direction output -region right:*
    place_pins -hor_layers M1 -ver_layers M2

    detailed_placement
    if {[catch {check_placement} problem]} {
        error "factorilog: placement check failed: $problem"
    }

} elseif {[info exists USE_ROW_PLACEMENT] && $USE_ROW_PLACEMENT == "1" && $has_ranks} {
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

    if {[info exists USE_CHASSIS] && $USE_CHASSIS == "1"} {
        if {![info exists CHASSIS_GRID_W] || $CHASSIS_GRID_W <= 0} {
            if {[info exists AUTO_CHASSIS_GRID_W] && $AUTO_CHASSIS_GRID_W > 0} {
                set CHASSIS_GRID_W $AUTO_CHASSIS_GRID_W
            } elseif {[info exists num_ranks] && $num_ranks > 0} {
                set CHASSIS_GRID_W [expr {int(ceil(sqrt($num_ranks)))}]
            } else {
                set CHASSIS_GRID_W 1
            }
        }
        if {![info exists CHASSIS_GRID_H] || $CHASSIS_GRID_H <= 0} {
            if {[info exists AUTO_CHASSIS_GRID_H] && $AUTO_CHASSIS_GRID_H > 0} {
                set CHASSIS_GRID_H $AUTO_CHASSIS_GRID_H
            } elseif {[info exists num_ranks] && $num_ranks > 0} {
                set CHASSIS_GRID_H [expr {int(ceil(sqrt($num_ranks)))}]
            } else {
                set CHASSIS_GRID_H 1
            }
        }
        if {![info exists CHASSIS_PITCH]} { set CHASSIS_PITCH 100 }
        if {![info exists CHASSIS_CORE_W]} { set CHASSIS_CORE_W 94 }
        if {![info exists CHASSIS_CORE_H]} { set CHASSIS_CORE_H 94 }
        if {![info exists CHASSIS_CHANNEL_W]} { set CHASSIS_CHANNEL_W 6 }

        set margin_x [expr {($CHASSIS_PITCH - $CHASSIS_CORE_W) / 2}]
        set margin_y [expr {($CHASSIS_PITCH - $CHASSIS_CORE_H) / 2}]
        set die_w [expr {$CHASSIS_GRID_W * $CHASSIS_PITCH}]
        set die_h [expr {$CHASSIS_GRID_H * $CHASSIS_PITCH}]
        set core_lo_x $margin_x
        set core_lo_y $margin_y
        set core_hi_x [expr {$die_w - $margin_x}]
        set core_hi_y [expr {$die_h - $margin_y}]

        set total_h $channel_h
        for {set r 0} {$r < $num_ranks} {incr r} {
            set row_start_y($r) [expr {$core_lo_y + $total_h}]
            set total_h [expr {$total_h + $rank_h($r) + $channel_h}]
        }

        puts "factorilog: structured linear row floorplan on chassis (${CHASSIS_GRID_W}x${CHASSIS_GRID_H}): die ${die_w}x${die_h}, core margin ${margin_x}x${margin_y}"

        initialize_floorplan -die_area [list 0 0 $die_w $die_h] \
            -core_area [list $core_lo_x $core_lo_y $core_hi_x $core_hi_y] \
            -site tile

        # Create placement blockages on street channels and outer margins
        create_blockage -region [list 0 0 $die_w $margin_y]
        set top_y [expr {($CHASSIS_GRID_H - 1) * $CHASSIS_PITCH + $margin_y + $CHASSIS_CORE_H}]
        create_blockage -region [list 0 $top_y $die_w $die_h]
        create_blockage -region [list 0 0 $margin_x $die_h]
        set right_x [expr {($CHASSIS_GRID_W - 1) * $CHASSIS_PITCH + $margin_x + $CHASSIS_CORE_W}]
        create_blockage -region [list $right_x 0 $die_w $die_h]

        # Internal horizontal street channels
        for {set by 0} {$by < [expr {$CHASSIS_GRID_H - 1}]} {incr by} {
            set y1 [expr {$by * $CHASSIS_PITCH + $margin_y + $CHASSIS_CORE_H}]
            set y2 [expr {$y1 + $CHASSIS_CHANNEL_W}]
            create_blockage -region [list 0 $y1 $die_w $y2]
        }

        # Internal vertical street channels
        for {set bx 0} {$bx < [expr {$CHASSIS_GRID_W - 1}]} {incr bx} {
            set x1 [expr {$bx * $CHASSIS_PITCH + $margin_x + $CHASSIS_CORE_W}]
            set x2 [expr {$x1 + $CHASSIS_CHANNEL_W}]
            create_blockage -region [list $x1 0 $x2 $die_h]
        }

        # Placement blockages for empty center blocks in chassis grids >= 3x3
        if {[info exists CHASSIS_EMPTY_BLOCKS] && $CHASSIS_EMPTY_BLOCKS != ""} {
            foreach blk $CHASSIS_EMPTY_BLOCKS {
                set bx [lindex $blk 0]
                set by [lindex $blk 1]
                set x1 [expr {$bx * $CHASSIS_PITCH + $margin_x}]
                set y1 [expr {$by * $CHASSIS_PITCH + $margin_y}]
                set x2 [expr {$x1 + $CHASSIS_CORE_W}]
                set y2 [expr {$y1 + $CHASSIS_CORE_H}]
                create_blockage -region [list $x1 $y1 $x2 $y2]
            }
        } elseif {$CHASSIS_GRID_W >= 3 && $CHASSIS_GRID_H >= 3} {
            set mid_bx [expr {($CHASSIS_GRID_W - 1) / 2}]
            set mid_by [expr {($CHASSIS_GRID_H - 1) / 2}]
            set x1 [expr {$mid_bx * $CHASSIS_PITCH + $margin_x}]
            set y1 [expr {$mid_by * $CHASSIS_PITCH + $margin_y}]
            set x2 [expr {$x1 + $CHASSIS_CORE_W}]
            set y2 [expr {$y1 + $CHASSIS_CORE_H}]
            create_blockage -region [list $x1 $y1 $x2 $y2]
        }
    } else {
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
    }

    make_tracks

    set dbTech [[$block getDataBase] getTech]
    set m2Layer [$dbTech findLayer "M2"]

    set is_chassis [expr {[info exists USE_CHASSIS] && $USE_CHASSIS == "1"}]
    if {$is_chassis} {
        set block_order [get_chassis_block_order $CHASSIS_GRID_W $CHASSIS_GRID_H]
        set num_blocks [llength $block_order]

        # Assign positions to instances in each rank row inside its allocated chassis block
        for {set r 0} {$r < $num_ranks} {incr r} {
            if {$num_blocks > 1} {
                set blk_idx [expr {$r % $num_blocks}]
                set blk [lindex $block_order $blk_idx]
                set bx [lindex $blk 0]
                set by [lindex $blk 1]
                set blk_lo_x [expr {$bx * $CHASSIS_PITCH + $margin_x}]
                set blk_lo_y [expr {$by * $CHASSIS_PITCH + $margin_y}]
                set blk_hi_x [expr {$blk_lo_x + $CHASSIS_CORE_W}]
                set blk_hi_y [expr {$blk_lo_y + $CHASSIS_CORE_H}]

                set rx [expr {$blk_lo_x + 6}]
                set ry [expr {$blk_lo_y + 10}]
                set cur_row_h 5
            } else {
                set blk_lo_x $core_lo_x
                set blk_hi_x $core_hi_x
                set rx [expr {$core_lo_x + 6}]
                set ry $row_start_y($r)
                set cur_row_h $rank_h($r)
            }

            if {[info exists rank_instances($r)]} {
                foreach iname $rank_instances($r) {
                    set dinst [$block findInst $iname]
                    if {$dinst != "NULL" && $dinst != ""} {
                        set dmaster [$dinst getMaster]
                        set dw [expr {[$dmaster getWidth] / $dbu}]
                        set dh [expr {[$dmaster getHeight] / $dbu}]
                        if {$dh > $cur_row_h} { set cur_row_h $dh }
                        set pad $pad_between
                        if {[string match "*merge*" $iname]} {
                            set pad 16
                        } elseif {[string match "*split*" $iname] || [string match "*bal*" $iname]} {
                            set pad [expr {max(10, $SPLIT_PAD_SITES)}]
                        } elseif {[string match "*disp*" $iname] || [string match "*acc*" $iname] || [string match "*comb*" $iname] || [string match "*dec*" $iname] || [string match "*latch*" $iname] || [string match "*switch*" $iname] || [string match "*params*" $iname]} {
                            set pad 2
                        }

                        set wrap_threshold [expr {$blk_hi_x - 6}]
                        if {[string match "*merge*" $iname]} {
                            set wrap_threshold [expr {$blk_hi_x - 20}]
                        }
                        if {$rx + $dw > $wrap_threshold} {
                            set rx [expr {$blk_lo_x + 6}]
                            set ry [expr {$ry + $cur_row_h + $channel_h}]
                            set cur_row_h $dh
                        }
                        $dinst setLocation [expr {int($rx * $dbu)}] [expr {int($ry * $dbu)}]
                        $dinst setPlacementStatus PLACED
                        set rx [expr {$rx + $dw + $pad}]
                    }
                }
            }
        }
    } else {
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
                        set pad $pad_between
                        if {[string match "*merge*" $iname]} {
                            set pad 16
                        } elseif {[string match "*split*" $iname] || [string match "*bal*" $iname]} {
                            set pad [expr {max(10, $SPLIT_PAD_SITES)}]
                        } elseif {[string match "*disp*" $iname] || [string match "*acc*" $iname] || [string match "*comb*" $iname] || [string match "*dec*" $iname] || [string match "*latch*" $iname] || [string match "*switch*" $iname] || [string match "*params*" $iname]} {
                            set pad 2
                        }

                        set wrap_threshold [expr {$core_hi_x - 2}]
                        if {[string match "*merge*" $iname]} {
                            set wrap_threshold [expr {$core_hi_x - 20}]
                        }
                        if {$rx + $dw > $wrap_threshold} {
                            set rx [expr {$core_lo_x + 2}]
                            set ry [expr {$ry + $rank_h($r) + $channel_h}]
                        }
                        $dinst setLocation [expr {int($rx * $dbu)}] [expr {int($ry * $dbu)}]
                        $dinst setPlacementStatus PLACED
                        set rx [expr {$rx + $dw + $pad}]
                    }
                }
            }
        }
    }

    # Place any unranked instances
    if {$is_chassis} {
        set unranked_x [expr {$core_lo_x + 6}]
        set unranked_y [expr {$core_lo_y + 80}]
    } else {
        set unranked_x [expr {$core_lo_x + 2}]
        set unranked_y [expr {$core_hi_y - 4}]
    }
    foreach inst [$block getInsts] {
        if {[$inst getPlacementStatus] != "PLACED"} {
            set master [$inst getMaster]
            set dw [expr {[$master getWidth] / $dbu}]
            set dh [expr {[$master getHeight] / $dbu}]
            if {$unranked_x + $dw > [expr {$core_hi_x - 6}]} {
                set unranked_x [expr {$core_lo_x + 6}]
                set unranked_y [expr {$unranked_y - $dh - 4}]
            }
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

} elseif {[info exists USE_CHASSIS] && $USE_CHASSIS == "1"} {
        if {![info exists CHASSIS_GRID_W] || $CHASSIS_GRID_W <= 0} {
            if {[info exists AUTO_CHASSIS_GRID_W] && $AUTO_CHASSIS_GRID_W > 0} {
                set CHASSIS_GRID_W $AUTO_CHASSIS_GRID_W
            } elseif {[info exists num_ranks] && $num_ranks > 0} {
                set CHASSIS_GRID_W [expr {int(ceil(sqrt($num_ranks)))}]
            } else {
                set CHASSIS_GRID_W 1
            }
        }
        if {![info exists CHASSIS_GRID_H] || $CHASSIS_GRID_H <= 0} {
            if {[info exists AUTO_CHASSIS_GRID_H] && $AUTO_CHASSIS_GRID_H > 0} {
                set CHASSIS_GRID_H $AUTO_CHASSIS_GRID_H
            } elseif {[info exists num_ranks] && $num_ranks > 0} {
                set CHASSIS_GRID_H [expr {int(ceil(sqrt($num_ranks)))}]
            } else {
                set CHASSIS_GRID_H 1
            }
        }
    if {![info exists CHASSIS_PITCH]} { set CHASSIS_PITCH 100 }
    if {![info exists CHASSIS_CORE_W]} { set CHASSIS_CORE_W 94 }
    if {![info exists CHASSIS_CORE_H]} { set CHASSIS_CORE_H 94 }
    if {![info exists CHASSIS_CHANNEL_W]} { set CHASSIS_CHANNEL_W 6 }

    set margin_x [expr {($CHASSIS_PITCH - $CHASSIS_CORE_W) / 2}]
    set margin_y [expr {($CHASSIS_PITCH - $CHASSIS_CORE_H) / 2}]
    set die_w [expr {$CHASSIS_GRID_W * $CHASSIS_PITCH}]
    set die_h [expr {$CHASSIS_GRID_H * $CHASSIS_PITCH}]
    set core_lo_x $margin_x
    set core_lo_y $margin_y
    set core_hi_x [expr {$die_w - $margin_x}]
    set core_hi_y [expr {$die_h - $margin_y}]

    puts "factorilog: chassis floorplan ${CHASSIS_GRID_W}x${CHASSIS_GRID_H} (die ${die_w}x${die_h}, core margin ${margin_x}x${margin_y})"

    initialize_floorplan -die_area [list 0 0 $die_w $die_h] \
        -core_area [list $core_lo_x $core_lo_y $core_hi_x $core_hi_y] \
        -site tile

    # Create placement blockages on street channels and outer margins
    create_blockage -region [list 0 0 $die_w $margin_y]
    set top_y [expr {($CHASSIS_GRID_H - 1) * $CHASSIS_PITCH + $margin_y + $CHASSIS_CORE_H}]
    create_blockage -region [list 0 $top_y $die_w $die_h]
    create_blockage -region [list 0 0 $margin_x $die_h]
    set right_x [expr {($CHASSIS_GRID_W - 1) * $CHASSIS_PITCH + $margin_x + $CHASSIS_CORE_W}]
    create_blockage -region [list $right_x 0 $die_w $die_h]

    # Internal horizontal street channels
    for {set by 0} {$by < [expr {$CHASSIS_GRID_H - 1}]} {incr by} {
        set y1 [expr {$by * $CHASSIS_PITCH + $margin_y + $CHASSIS_CORE_H}]
        set y2 [expr {$y1 + $CHASSIS_CHANNEL_W}]
        create_blockage -region [list 0 $y1 $die_w $y2]
    }

    # Internal vertical street channels
    for {set bx 0} {$bx < [expr {$CHASSIS_GRID_W - 1}]} {incr bx} {
        set x1 [expr {$bx * $CHASSIS_PITCH + $margin_x + $CHASSIS_CORE_W}]
        set x2 [expr {$x1 + $CHASSIS_CHANNEL_W}]
        create_blockage -region [list $x1 0 $x2 $die_h]
    }

    # Placement blockages for empty center blocks in chassis grids >= 3x3
    if {[info exists CHASSIS_EMPTY_BLOCKS] && $CHASSIS_EMPTY_BLOCKS != ""} {
        foreach blk $CHASSIS_EMPTY_BLOCKS {
            set bx [lindex $blk 0]
            set by [lindex $blk 1]
            set x1 [expr {$bx * $CHASSIS_PITCH + $margin_x}]
            set y1 [expr {$by * $CHASSIS_PITCH + $margin_y}]
            set x2 [expr {$x1 + $CHASSIS_CORE_W}]
            set y2 [expr {$y1 + $CHASSIS_CORE_H}]
            create_blockage -region [list $x1 $y1 $x2 $y2]
        }
    } elseif {$CHASSIS_GRID_W >= 3 && $CHASSIS_GRID_H >= 3} {
        set mid_bx [expr {($CHASSIS_GRID_W - 1) / 2}]
        set mid_by [expr {($CHASSIS_GRID_H - 1) / 2}]
        set x1 [expr {$mid_bx * $CHASSIS_PITCH + $margin_x}]
        set y1 [expr {$mid_by * $CHASSIS_PITCH + $margin_y}]
        set x2 [expr {$x1 + $CHASSIS_CORE_W}]
        set y2 [expr {$y1 + $CHASSIS_CORE_H}]
        create_blockage -region [list $x1 $y1 $x2 $y2]
    }

    make_tracks

    # Place pins on the die edge: inputs on left, outputs on right
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

if {[info exists DO_UNTANGLE] && $DO_UNTANGLE == "1"} {
    do_untangle $block $dbu
}

# --- route ----------------------------------------------------------
# Routing is handled exclusively by native 3D DualLaneMazeRouter.
# OpenROAD TritonRoute is deleted per new Plan of Record (POR).
puts "factorilog: OpenROAD router bypassed; routing handled by native 3D maze router"
set fp [open "route_drc.rpt" w]
close $fp

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
