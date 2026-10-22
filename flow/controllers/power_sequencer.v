// Power sequencer and hysteresis FSM controller for Factorio factories.
// ASD-STE100 technical comments.

`default_nettype none

module power_sequencer (
    input logic clk,  // Clock signal
    input logic rst_n,  // Active-low reset signal
    input logic [31:0] accumulator_charge,  // Accumulator charge percentage
    input logic [31:0] buffer_level,  // Steam or fluid buffer level
    output logic power_switch_enable,  // Main factory power switch control
    output logic backup_pump_enable,  // Backup steam pump control
    output logic [31:0] alert_code  // Telemetry alert status code
);
    // State definitions
    localparam logic [1:0] STATE_NORMAL    = 2'b00;
    localparam logic [1:0] STATE_THROTTLED = 2'b01;
    localparam logic [1:0] STATE_EMERGENCY = 2'b10;

    // Charge threshold constants
    localparam logic [31:0] THRESH_THROTTLED         = 32'd30;
    localparam logic [31:0] THRESH_RECOVER_NORMAL    = 32'd70;
    localparam logic [31:0] THRESH_EMERGENCY         = 32'd10;
    localparam logic [31:0] THRESH_RECOVER_THROTTLED = 32'd20;

    logic [1:0] state;  // Current hysteresis FSM state
    logic [1:0] next_state;  // Next hysteresis FSM state

    always_comb begin
        next_state = state;
        case (state)
            STATE_NORMAL: begin
                if (accumulator_charge < THRESH_THROTTLED)
                    next_state = STATE_THROTTLED;
            end
            STATE_THROTTLED: begin
                if (accumulator_charge > THRESH_RECOVER_NORMAL)
                    next_state = STATE_NORMAL;
                else if (accumulator_charge < THRESH_EMERGENCY)
                    next_state = STATE_EMERGENCY;
            end
            STATE_EMERGENCY: begin
                if (accumulator_charge > THRESH_RECOVER_THROTTLED)
                    next_state = STATE_THROTTLED;
            end
            default: begin
                next_state = STATE_NORMAL;
            end
        endcase
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= STATE_NORMAL;
            power_switch_enable <= 1'b1;
            backup_pump_enable <= 1'b0;
            alert_code <= 32'd0;
        end else begin
            state <= next_state;
            power_switch_enable <= (next_state != STATE_EMERGENCY);
            backup_pump_enable <= (next_state != STATE_NORMAL);
            alert_code <= {30'd0, next_state};
        end
    end
endmodule
`default_nettype wire
