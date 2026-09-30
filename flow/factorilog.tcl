# factorilog.tcl - the OpenROAD stage of the Factorilog flow.
#
# Reads the PDK and the flat netlist, links the design, and writes the
# DEF that the game bridge consumes. Floorplan, place, and route join
# this file as the PDK grows: this is the skeleton.
#
# Run from foundry/ (see flow/orfs.sh):
#   openroad -no_init -exit flow/factorilog.tcl

read_liberty tech.lib
read_lef tech.lef
read_verilog netlist.v
link_design factory

set block [ord::get_db_block]
puts "factorilog: [llength [$block getInsts]] instances, [llength [$block getNets]] nets, [llength [$block getBTerms]] ports"

write_def factory.def
puts "factorilog: DEF written"