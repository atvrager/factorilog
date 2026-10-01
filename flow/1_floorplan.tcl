# 1_floorplan.tcl - Floorplan and pin placement stage.
#
# Reads the PDK LEF, Liberty, and structural netlist.
# Computes die dimensions from target block fill.
# Generates routing tracks and places perimeter pins.

set BLOCK_FILL 0.28
set CORE_MARGIN 4

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
place_pins -hor_layers M1 -ver_layers M2

write_db 1_floorplan.odb
puts "factorilog: floorplan stage complete, wrote 1_floorplan.odb"
