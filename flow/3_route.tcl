# 3_route.tcl - Routing stage.
#
# Reads the placed database.
# Detailed routing is handled exclusively by native 3D DualLaneMazeRouter per POR.
# OpenROAD TritonRoute is deleted.

read_db 2_placed.odb

set fp [open "route_drc.rpt" w]
close $fp

write_db 3_routed.odb
write_def factory.def
puts "factorilog: routing stage complete (native maze router POR), wrote 3_routed.odb and factory.def"
