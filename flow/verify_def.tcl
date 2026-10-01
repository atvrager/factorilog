# verify_def.tcl - load factory.def back and prove it is sound.
#
# The game bridge reads factory.def, so the file must survive a reload
# by the same reader. This stage reads the PDK, reads the DEF, and
# checks the numbers the bridge depends on: every block placed on the
# tile grid, every port on the die edge, no overlap.
#
# usage: flow/orfs.sh flow/verify_def.tcl

read_liberty tech.lib
read_lef tech.lef
read_def factory.def

set block [ord::get_db_block]
set dbu [[ord::get_db] getDbuPerMicron]
set die [$block getDieArea]

set insts [$block getInsts]
set placed 0
foreach inst $insts {
    if {[$inst getPlacementStatus] == "PLACED"} {
        incr placed
    }
}
if {$placed != [llength $insts]} {
    error "verify: [llength $insts] blocks, only $placed placed"
}

# A port outside the die cannot exist in the game: the blueprint would
# place an input off the buildable area.
set offdie 0
foreach term [$block getBTerms] {
    set box [$term getBBox]
    if {[$box xMin] < [$die xMin] || [$box xMax] > [$die xMax] ||
        [$box yMin] < [$die yMin] || [$box yMax] > [$die yMax]} {
        incr offdie
    }
}
if {$offdie > 0} {
    error "verify: $offdie ports outside the die"
}

if {[catch {check_placement} problem]} {
    error "verify: placement check failed: $problem"
}

set routed 0
foreach net [$block getNets] {
    if {[$net getWire] != "NULL"} {
        incr routed
    }
}

set die_w [expr {([$die xMax] - [$die xMin]) / $dbu}]
set die_h [expr {([$die yMax] - [$die yMin]) / $dbu}]
puts "verify: die $die_w x $die_h tiles, [llength $insts] blocks placed,\
 $routed routed nets, [llength [$block getNets]] nets, [llength [$block getBTerms]] ports"

# --- timing ---------------------------------------------------------
# Static timing analysis across the factory combinational paths.
# Virtual clock anchors the raw inputs and science pack outputs.
create_clock -name virtual_clk -period 1000
set_input_delay -clock virtual_clk 0 [all_inputs]
set_output_delay -clock virtual_clk 0 [all_outputs]
report_checks -path_delay max -fields {input_pins slew capacitance} -digits 4
report_worst_slack -max
