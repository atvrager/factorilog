# 3_route.tcl - Global and detailed routing stage.
#
# Reads the placed database.
# Runs FastRoute and TritonRoute to connect all signal tracks.
# Emits the routed database and DEF file.

read_db 2_placed.odb

set_thread_count [exec nproc]
set_routing_layers -signal M1-M6
global_route -verbose
detailed_route -output_drc route_drc.rpt -verbose 1

write_db 3_routed.odb
write_def factory.def
puts "factorilog: routing stage complete, wrote 3_routed.odb and factory.def"
