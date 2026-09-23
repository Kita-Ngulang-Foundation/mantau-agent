package id.mantau.agent.inference.fall

import kotlin.math.abs

/**
 * Decides, per frame, how pose should run: the Android counterpart of the Python
 * agent's motion gate with its idle keepalive.
 *
 * While the scene moves (and for [holdMs] after), pose runs on every frame in video
 * (tracking) mode. Once it has been still that long, pose runs only every [idleIntervalMs]
 * and statelessly (image mode): video-mode tracking can keep a "ghost" pose on a static
 * scene after the person has left, which would hide that the room is empty.
 *
 * Motion is judged on a small grayscale thumbnail: the fraction of pixels whose
 * brightness changed by more than [pixelThreshold] since the previous frame.
 */
class IdleGate(
    private val holdMs: Long = 3_000,
    private val idleIntervalMs: Long = 500,
    private val pixelThreshold: Int = 25,
    private val changedFraction: Double = 0.005,
) {
    enum class Pose { TRACK, SNAPSHOT, SKIP }

    private var previous: IntArray? = null
    private var lastMotionMs: Long? = null
    private var lastSnapshotMs: Long? = null

    /** [luma] is the thumbnail's brightness, 0..255 per pixel, same size every frame. */
    fun update(luma: IntArray, timestampMs: Long): Pose {
        val prev = previous
        previous = luma
        if (prev == null || prev.size != luma.size || moved(prev, luma)) lastMotionMs = timestampMs
        val sinceMotion = timestampMs - (lastMotionMs ?: timestampMs)
        if (sinceMotion < holdMs) {
            lastSnapshotMs = null
            return Pose.TRACK
        }
        val last = lastSnapshotMs
        if (last != null && timestampMs - last < idleIntervalMs) return Pose.SKIP
        lastSnapshotMs = timestampMs
        return Pose.SNAPSHOT
    }

    private fun moved(a: IntArray, b: IntArray): Boolean {
        var changed = 0
        for (i in a.indices) if (abs(a[i] - b[i]) > pixelThreshold) changed++
        return changed > changedFraction * a.size
    }

    companion object {
        const val THUMB_WIDTH = 64
        const val THUMB_HEIGHT = 48
    }
}
