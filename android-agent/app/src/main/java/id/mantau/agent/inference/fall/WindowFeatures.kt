package id.mantau.agent.inference.fall

import java.math.BigDecimal
import java.math.RoundingMode

/** One frame of one tracked person: a port of mantau-AI `mantau.ml.features.FrameSample`. */
data class FrameSample(
    val t: Double,
    val cy: Double,
    val cx: Double,
    val aspectRatio: Double,
    val torsoAngleDeg: Double,
    val headHeightRel: Double,
) {
    companion object {
        fun of(person: PersonPose, t: Double) = FrameSample(
            t = t,
            cy = person.centroidY,
            cx = person.centroidX,
            aspectRatio = person.aspectRatio,
            torsoAngleDeg = person.torsoAngleDeg,
            headHeightRel = person.headHeightRel,
        )
    }
}

/**
 * The classifier's feature vector: a port of mantau-AI `mantau.ml.features.window_features`.
 * Order and arithmetic must match the Python source exactly (the ONNX model was trained on it).
 */
object WindowFeatures {
    val NAMES = listOf(
        "duration_s", "n_samples", "sample_rate", "cy_start", "cy_end", "cy_delta", "cy_min",
        "cy_max", "cy_range", "vel_net", "vel_peak", "ar_start", "ar_end", "ar_min", "ar_max",
        "ar_mean", "ar_delta", "angle_end", "angle_min", "angle_max", "angle_mean", "angle_delta",
        "head_start", "head_end", "head_min", "head_mean", "cx_range", "cx_abs_delta",
    )
    val SIZE = NAMES.size

    fun compute(samples: List<FrameSample>): FloatArray {
        val n = samples.size
        if (n == 0) return FloatArray(SIZE)
        val t = DoubleArray(n) { samples[it].t }
        val cy = DoubleArray(n) { samples[it].cy }
        val cx = DoubleArray(n) { samples[it].cx }
        val ar = DoubleArray(n) { samples[it].aspectRatio }
        val ang = DoubleArray(n) { samples[it].torsoAngleDeg }
        val head = DoubleArray(n) { samples[it].headHeightRel }
        val duration = t[n - 1] - t[0]

        var velPeak = 0.0
        if (n >= 2) {
            velPeak = Double.NEGATIVE_INFINITY
            for (i in 1 until n) {
                val dt = t[i] - t[i - 1]
                val step = if (dt > 1e-6) (cy[i] - cy[i - 1]) / dt else 0.0
                if (step > velPeak) velPeak = step
            }
        }
        val features = doubleArrayOf(
            duration,
            n.toDouble(),
            safeDiv(n.toDouble(), duration),
            cy[0],
            cy[n - 1],
            cy[n - 1] - cy[0],
            cy.min(),
            cy.max(),
            cy.max() - cy.min(),
            safeDiv(cy[n - 1] - cy[0], duration),
            velPeak,
            ar[0],
            ar[n - 1],
            ar.min(),
            ar.max(),
            NumpyMath.mean(ar),
            ar[n - 1] - ar[0],
            ang[n - 1],
            ang.min(),
            ang.max(),
            NumpyMath.mean(ang),
            ang[n - 1] - ang[0],
            head[0],
            head[n - 1],
            head.min(),
            NumpyMath.mean(head),
            cx.max() - cx.min(),
            kotlin.math.abs(cx[n - 1] - cx[0]),
        )
        check(features.size == SIZE)
        return FloatArray(SIZE) { features[it].toFloat() }
    }

    private fun safeDiv(a: Double, b: Double): Double = if (kotlin.math.abs(b) > 1e-9) a / b else 0.0
}

/** The few numeric behaviors of Python/numpy the fall rules depend on. */
object NumpyMath {
    /** Python's `round(x, digits)`: exact binary value, ties to even. */
    fun round(x: Double, digits: Int): Double =
        BigDecimal(x).setScale(digits, RoundingMode.HALF_EVEN).toDouble()

    /** `numpy.mean` for float64: pairwise summation (same blocking as numpy) / n. */
    fun mean(values: DoubleArray): Double = pairwiseSum(values, 0, values.size) / values.size

    private fun pairwiseSum(a: DoubleArray, start: Int, n: Int): Double {
        if (n < 8) {
            var res = -0.0
            for (i in 0 until n) res += a[start + i]
            return res
        }
        if (n <= BLOCK) {
            val r = DoubleArray(8) { a[start + it] }
            var i = 8
            while (i < n - (n % 8)) {
                for (k in 0 until 8) r[k] += a[start + i + k]
                i += 8
            }
            var res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]))
            while (i < n) {
                res += a[start + i]
                i++
            }
            return res
        }
        var n2 = n / 2
        n2 -= n2 % 8
        return pairwiseSum(a, start, n2) + pairwiseSum(a, start + n2, n - n2)
    }

    private const val BLOCK = 128
}
