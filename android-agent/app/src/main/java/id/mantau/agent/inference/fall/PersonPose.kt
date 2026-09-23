package id.mantau.agent.inference.fall

import kotlin.math.acos
import kotlin.math.sqrt

/**
 * One person's pose in one frame: a port of mantau-AI `mantau.stages.pose.PersonPose`.
 *
 * Arithmetic deliberately mirrors the Python implementation, including where numpy
 * works in float32 (landmark sums, the torso vector) and where Python works in double,
 * so the recorded pose-sequence fixtures produce identical fall decisions on both
 * platforms. Do not "simplify" the Float/Double mix.
 */
class PersonPose(
    /** 33 x [x, y, z, visibility]; x/y normalized to the image. */
    val landmarks: Array<FloatArray>,
    /** 33 x [x, y, z] meters, hip-centered (orientation only). */
    val world: Array<FloatArray>,
    val imageHeight: Int,
    val imageWidth: Int,
) {
    init {
        require(landmarks.size == LANDMARKS && landmarks.all { it.size == 4 })
        require(world.size == LANDMARKS && world.all { it.size == 3 })
    }

    /** (x1, y1, x2, y2) pixels around the visible landmarks. */
    val bbox: IntArray = bboxFromLandmarks(landmarks, imageWidth, imageHeight)

    /** Hip midpoint in normalized image coordinates. */
    val centroidX: Double = ((landmarks[L_HIP][0] + landmarks[R_HIP][0]) / 2f).toDouble()
    val centroidY: Double = ((landmarks[L_HIP][1] + landmarks[R_HIP][1]) / 2f).toDouble()

    val aspectRatio: Double
        get() = (bbox[2] - bbox[0]).toDouble() / maxOf(bbox[3] - bbox[1], 1)

    /** (hip_y - nose_y) * frame_h / bbox_h: + head above hips, - face-down. */
    val headHeightRel: Double
        get() {
            val bboxH = maxOf(bbox[3] - bbox[1], 1)
            return (centroidY - landmarks[NOSE][1].toDouble()) * imageHeight / bboxH
        }

    /** Torso (hip to shoulder) angle from vertical in degrees; 0 upright, 90 horizontal. */
    val torsoAngleDeg: Double
        get() {
            val v = FloatArray(3) { i ->
                (world[L_SHOULDER][i] + world[R_SHOULDER][i]) / 2f -
                    (world[L_HIP][i] + world[R_HIP][i]) / 2f
            }
            val n = sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
            if (n < 1e-6f) return 0.0
            // dot(v / n, (0, -1, 0)) in double, as numpy upcasts against the float64 axis.
            val cos = (-(v[1] / n).toDouble()).coerceIn(-1.0, 1.0)
            return acos(cos) * (180.0 / Math.PI)
        }

    companion object {
        const val LANDMARKS = 33
        const val NOSE = 0
        const val L_SHOULDER = 11
        const val R_SHOULDER = 12
        const val L_HIP = 23
        const val R_HIP = 24
        const val L_KNEE = 25
        const val R_KNEE = 26
        private const val VISIBLE = 0.3f

        fun bboxFromLandmarks(landmarks: Array<FloatArray>, width: Int, height: Int): IntArray {
            val visible = landmarks.filter { it[3] >= VISIBLE }.ifEmpty { landmarks.toList() }
            val maxX = (width - 1).toFloat()
            val maxY = (height - 1).toFloat()
            val xs = visible.map { (it[0] * width).coerceIn(0f, maxX) }
            val ys = visible.map { (it[1] * height).coerceIn(0f, maxY) }
            return intArrayOf(xs.min().toInt(), ys.min().toInt(), xs.max().toInt(), ys.max().toInt())
        }
    }
}
