# 4_sta.tcl - Static timing analysis stage.
#
# Reads the routed database and Liberty timing tables.
# Analyzes recipe craft delays and Slack margins.

read_db 3_routed.odb
read_liberty tech.lib

set_thread_count [exec nproc]

create_clock -name virtual_clk -period 1000
set_input_delay -clock virtual_clk 0 [all_inputs]
if {[llength [all_outputs]] > 0} {
    set_output_delay -clock virtual_clk 0 [all_outputs]
}

report_checks -path_delay max -fields {input_pins slew capacitance} -digits 4 > timing.rpt
report_worst_slack -max >> timing.rpt
puts "factorilog: timing analysis stage complete"
