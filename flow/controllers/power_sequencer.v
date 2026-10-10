// Power sequencer and hysteresis FSM controller for Factorio factories.
// ASD-STE100 technical comments.

module power_sequencer (
    input wire clk,
    input wire rst_n,
    input wire [31:0] accumulator_charge,
    input wire [31:0] buffer_level,
    output reg power_switch_enable,
    output reg backup_pump_enable,
    output reg [31:0] alert_code
);
    // State definitions
    localparam STATE_NORMAL    = 2'b00;
    localparam STATE_THROTTLED = 2'b01;
    localparam STATE_EMERGENCY = 2'b10;

    reg [1:0] state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= STATE_NORMAL;
            power_switch_enable <= 1'b1;
            backup_pump_enable <= 1'b0;
            alert_code <= 32'd0;
        end else begin
            case (state)
                STATE_NORMAL: begin
                    power_switch_enable <= 1'b1;
                    backup_pump_enable <= 1'b0;
                    alert_code <= 32'd0;
                    if (accumulator_charge < 32'd30)
                        state <= STATE_THROTTLED;
                end
                STATE_THROTTLED: begin
                    power_switch_enable <= 1'b1;
                    backup_pump_enable <= 1'b1;
                    alert_code <= 32'd1;
                    if (accumulator_charge > 32'd70)
                        state <= STATE_NORMAL;
                    else if (accumulator_charge < 32'd10)
                        state <= STATE_EMERGENCY;
                end
                STATE_EMERGENCY: begin
                    power_switch_enable <= 1'b0;
                    backup_pump_enable <= 1'b1;
                    alert_code <= 32'd2;
                    if (accumulator_charge > 32'd20)
                        state <= STATE_THROTTLED;
                end
                default: begin
                    state <= STATE_NORMAL;
                    power_switch_enable <= 1'b1;
                    backup_pump_enable <= 1'b0;
                    alert_code <= 32'd0;
                end
            endcase
        end
    end
endmodule
