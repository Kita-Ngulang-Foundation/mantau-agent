package id.mantau.agent.activity

import java.math.BigDecimal
import java.math.RoundingMode

/** The numeric behavior of Python the activity rules depend on. */
object NumpyMath {
    /** Python's `round(x, digits)`: exact binary value, ties to even. */
    fun round(x: Double, digits: Int): Double =
        BigDecimal(x).setScale(digits, RoundingMode.HALF_EVEN).toDouble()
}
