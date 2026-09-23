package id.mantau.agent.inference.fall

import kotlin.math.hypot

enum class TrackState(val wire: String) { NORMAL("normal"), FALLING("falling"), FALLEN("fallen") }

/** Parameters of the fall rules; defaults are mantau-AI config/default.yaml `fall:`. */
data class FallConfig(
    val windowSeconds: Double = 1.5,
    val dropVelocity: Double = 0.45,
    val aspectRatioHorizontal: Double = 1.2,
    val torsoAngleHorizontal: Double = 70.0,
    val timeOnGround: Double = 1.0,
    val trackMatchDistance: Double = 0.15,
    val trackMaxAge: Double = 1.0,
    val centroidFallenY: Double = 0.0,
    val headHeightFallen: Double = -0.50,
)

/** Learned confirmation layer: `confirms(features) -> (confirmed, probability)`. */
fun interface FallConfirmer {
    fun confirms(features: FloatArray): Pair<Boolean, Double>
}

data class FallDecision(
    val trackId: Int,
    val timestampMs: Long,
    val confidence: Double,
    val velocity: Double,
    val aspectRatio: Double,
    val torsoAngleDeg: Double,
)

data class TrackUpdate(val trackId: Int, val state: TrackState, val bbox: IntArray, val confidence: Double)

/**
 * The rule-based fall state machine: a line-for-line port of mantau-AI
 * `mantau.stages.fall.FallDetector` (greedy IoU/centroid tracker, NORMAL -> FALLING ->
 * FALLEN per track, optional learned confirmation). The shared pose-sequence fixtures
 * in mantau-core pin both implementations to identical decisions.
 */
class FallDetector(
    private val config: FallConfig = FallConfig(),
    private val confirmer: FallConfirmer? = null,
) {
    private class Track(val id: Int) {
        val history = ArrayDeque<FrameSample>()
        var state = TrackState.NORMAL
        var lastSeenS = 0.0
        var horizontalSinceS: Double? = null
        var fired = false
        var lastCentroid = 0.5 to 0.5
        var lastBbox = intArrayOf(0, 0, 0, 0)
        var fastDrop = false
    }

    private val tracks = LinkedHashMap<Int, Track>()
    private var nextId = 0

    fun update(people: List<PersonPose>, timestampMs: Long): Pair<List<TrackUpdate>, List<FallDecision>> {
        val t = timestampMs / 1000.0
        val events = mutableListOf<FallDecision>()
        val updates = mutableListOf<TrackUpdate>()
        for ((tid, person) in match(people)) {
            val tr = tracks.getValue(tid)
            val ar = person.aspectRatio
            val angle = person.torsoAngleDeg
            tr.lastCentroid = person.centroidX to person.centroidY
            tr.lastBbox = person.bbox
            tr.lastSeenS = t
            tr.history.addLast(FrameSample.of(person, t))
            if (tr.history.size > HISTORY) tr.history.removeFirst()

            val velocity = verticalVelocity(tr)
            val hadDrop = velocity >= config.dropVelocity
            if (hadDrop) tr.fastDrop = true
            val isHorizontal = ar >= config.aspectRatioHorizontal || angle >= config.torsoAngleHorizontal
            val headOk = person.headHeightRel >= config.headHeightFallen || tr.fastDrop
            val isGrounded = isHorizontal && person.centroidY >= config.centroidFallenY && headOk

            if (isGrounded) {
                val since = tr.horizontalSinceS ?: t.also { tr.horizontalSinceS = it }
                val groundedFor = t - since
                if (tr.state == TrackState.NORMAL && (hadDrop || recentDrop(tr))) tr.state = TrackState.FALLING
                if (tr.state == TrackState.FALLING && groundedFor >= config.timeOnGround && !tr.fired) {
                    val (confirmed, conf) = confirm(tr, velocity, ar, angle)
                    if (confirmed) {
                        tr.state = TrackState.FALLEN
                        tr.fired = true
                        events += FallDecision(tid, timestampMs, conf, velocity, ar, angle)
                    }
                }
            } else if (isHorizontal) {
                tr.horizontalSinceS = null
            } else {
                tr.horizontalSinceS = null
                tr.state = TrackState.NORMAL
                tr.fired = false
                tr.fastDrop = false
            }
            updates += TrackUpdate(tid, tr.state, person.bbox, confidence(velocity, ar, angle))
        }
        prune(t)
        return updates to events
    }

    private fun match(people: List<PersonPose>): List<Pair<Int, PersonPose>> {
        val assignments = mutableListOf<Pair<Int, PersonPose>>()
        val used = mutableSetOf<Int>()
        for (person in people) {
            var bestId: Int? = null
            var bestScore = 0.0
            for ((tid, tr) in tracks) {
                if (tid in used) continue
                val iou = iou(person.bbox, tr.lastBbox)
                val d = hypot(person.centroidX - tr.lastCentroid.first, person.centroidY - tr.lastCentroid.second)
                val score = if (iou > 0.1) iou
                else if (d < config.trackMatchDistance) 1.0 - d / config.trackMatchDistance
                else 0.0
                if (score > bestScore) {
                    bestId = tid
                    bestScore = score
                }
            }
            val id = bestId ?: nextId++.also { tracks[it] = Track(it) }
            used += id
            assignments += id to person
        }
        return assignments
    }

    private fun window(tr: Track): List<FrameSample> {
        if (tr.history.isEmpty()) return emptyList()
        val tNow = tr.history.last().t
        return tr.history.filter { tNow - it.t <= config.windowSeconds }
    }

    private fun verticalVelocity(tr: Track): Double {
        val w = window(tr)
        if (w.size < 2) return 0.0
        val dt = w.last().t - w.first().t
        if (dt <= 1e-6) return 0.0
        return (w.last().cy - w.first().cy) / dt
    }

    private fun recentDrop(tr: Track): Boolean {
        val w = window(tr)
        for (i in 1 until w.size) {
            val dt = w[i].t - w[i - 1].t
            if (dt > 1e-6 && (w[i].cy - w[i - 1].cy) / dt >= config.dropVelocity) return true
        }
        return false
    }

    private fun confirm(tr: Track, velocity: Double, ar: Double, angle: Double): Pair<Boolean, Double> {
        val ruleConfidence = confidence(velocity, ar, angle)
        val confirmer = confirmer ?: return true to ruleConfidence
        val (ok, prob) = confirmer.confirms(WindowFeatures.compute(window(tr)))
        return ok to NumpyMath.round(prob, 3)
    }

    private fun confidence(velocity: Double, ar: Double, angle: Double): Double {
        val v = (velocity / config.dropVelocity).coerceIn(0.0, 1.0)
        val a = (ar / config.aspectRatioHorizontal).coerceIn(0.0, 1.0)
        val o = (angle / config.torsoAngleHorizontal).coerceIn(0.0, 1.0)
        return NumpyMath.round(0.4 * v + 0.3 * a + 0.3 * o, 3)
    }

    private fun prune(tNow: Double) {
        tracks.entries.removeIf { tNow - it.value.lastSeenS > config.trackMaxAge }
    }

    companion object {
        private const val HISTORY = 256

        fun iou(a: IntArray, b: IntArray): Double {
            val iw = maxOf(minOf(a[2], b[2]) - maxOf(a[0], b[0]), 0)
            val ih = maxOf(minOf(a[3], b[3]) - maxOf(a[1], b[1]), 0)
            val inter = iw * ih
            if (inter == 0) return 0.0
            val areaA = maxOf(a[2] - a[0], 0) * maxOf(a[3] - a[1], 0)
            val areaB = maxOf(b[2] - b[0], 0) * maxOf(b[3] - b[1], 0)
            val union = areaA + areaB - inter
            return if (union > 0) inter.toDouble() / union else 0.0
        }
    }
}
