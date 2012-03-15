//for an explanation of this design pattery see:
var QAUtils = new function() {
    this.ACT_LOW = "act_low";
    this.TOL_LOW = "tol_low";
    this.TOL_HIGH = "tol_high";
    this.ACT_HIGH = "act_high";

    this.WITHIN_TOL = "ok";
    this.TOLERANCE = "tolerance";
    this.ACTION = "action";

    this.RELATIVE = "relative";
    this.ABSOLUTE = "absolute";

    this.EPSILON = 1E-10;



    /***************************************************************/
    /* Tolerance functions
    value is floating point number to be tested
    reference is a floating point number
    while tolerances must be an object with act_low, tol_low, tol_high,
    act_high and tol_type attributes.
    */
    this.percent_difference = function(measured, reference){
            //reference = 0. is a special case
            if (Math.abs(reference.value) < this.EPSILON){
                return absolute_difference(measured,reference);
            }
            return 100.*(measured-reference)/reference;
    };

    this.absolute_difference = function(measured,reference){
            return measured - reference;
    };

    this.test_tolerance = function(value, reference, tolerances){
        //compare a value to a reference value and check whether it is
        //within tolerances or not.
        //Return an object with a 'diff' and 'result' value
        var diff;
        var status;
        var message;

        if (tolerances.type == this.RELATIVE){
            diff = this.percent_difference(value,reference);
            message = "(" + diff.toFixed(1)+")";

        }else{
            diff = this.absolute_difference(value,reference);
            message = "(" + diff.toFixed(2)+")";
        }

        if ((tolerances.tol_low <= diff) && (diff <= tolerances.tol_high)){
            status = this.WITHIN_TOL;
            gen_status = this.WITHIN_TOL;
            message = "OK" + message;
        }else if ((tolerances.act_low <= diff) && (diff <= tolerances.tol_low)){
            status = this.TOL_LOW;
            gen_status = this.TOLERANCE;
            message = "TOL" + message;
        }else if ((tolerances.tol_high <= diff) && (diff <= tolerances.act_high)){
            status = this.TOL_HIGH;
            message = "TOL" + message;
            gen_status = this.TOLERANCE;
        }else if (diff <= tolerances.act_low){
            status = this.ACT_LOW;
            message = "ACT" + message;
            gen_status = this.ACTION;
        }else{
            status = this.ACT_HIGH;
            message = "ACT" + message;
            gen_status = this.ACTION;
        }

        return {status:status, gen_status:gen_status, diff:diff, message:message};
    }

}();