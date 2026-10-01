# 2_place.tcl - Global and detailed placement stage.
#
# Reads the floorplan database.
# Applies cell padding and legalizes instance positions.

read_db 1_floorplan.odb

set block [ord::get_db_block]
set CELL_PAD_SITES 1

set density [expr {[gpl::get_global_placement_uniform_density \
    -pad_left $CELL_PAD_SITES -pad_right $CELL_PAD_SITES] + 0.01}]
puts "factorilog: placement density [format %.3f $density]"

global_placement -density $density \
    -pad_left $CELL_PAD_SITES \
    -pad_right $CELL_PAD_SITES

set_placement_padding -global -left $CELL_PAD_SITES -right $CELL_PAD_SITES
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

write_db 2_placed.odb
puts "factorilog: placement stage complete, wrote 2_placed.odb"
