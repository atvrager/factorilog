# 1_floorplan.tcl - Floorplan and pin placement stage.
#
# Reads the PDK LEF, Liberty, and structural netlist.
# Computes die dimensions from target block fill.
# Generates routing tracks and places perimeter pins.

set BLOCK_FILL 0.04
set CORE_MARGIN 6

read_liberty tech.lib
read_lef tech.lef
read_verilog netlist.v
link_design factory

set block [ord::get_db_block]
set dbu [[ord::get_db] getDbuPerMicron]

set block_area 0
foreach inst [$block getInsts] {
    set master [$inst getMaster]
    set w [expr {[$master getWidth] / $dbu}]
    set h [expr {[$master getHeight] / $dbu}]
    set block_area [expr {$block_area + $w * $h}]
}

if {[info exists USE_CHASSIS] && $USE_CHASSIS == "1"} {
    if {![info exists CHASSIS_GRID_W]} { set CHASSIS_GRID_W 1 }
    if {![info exists CHASSIS_GRID_H]} { set CHASSIS_GRID_H 1 }
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
}

make_tracks
place_pins -hor_layers M1 -ver_layers M2

write_db 1_floorplan.odb
puts "factorilog: floorplan stage complete, wrote 1_floorplan.odb"
