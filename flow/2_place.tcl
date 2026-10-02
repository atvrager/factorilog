# 2_place.tcl - Global and detailed placement stage.
#
# Reads the floorplan database.
# Applies cell padding and legalizes instance positions.

read_db 1_floorplan.odb

set_thread_count [exec nproc]
set block [ord::get_db_block]
set CELL_PAD_SITES 6

set density 0.10
puts "factorilog: placement density [format %.3f $density]"

global_placement -density $density \
    -pad_left $CELL_PAD_SITES \
    -pad_right $CELL_PAD_SITES \
    -routability_driven

place_pins -hor_layers M1 -ver_layers M2
set_placement_padding -global -left $CELL_PAD_SITES -right $CELL_PAD_SITES
set_placement_padding -masters {belt_merge2 belt_split2} -left 5 -right 5
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
