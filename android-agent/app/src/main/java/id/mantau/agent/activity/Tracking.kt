package id.mantau.agent.activity

import id.mantau.agent.inference.fall.PersonObservation
import java.time.Instant
import java.time.temporal.ChronoUnit
import kotlin.math.hypot

/**
 * Port of mantau-core `activity.tracking`: stable local ids (position and time only, no
 * biometrics), a confidence floor, and timers that pause across outages, gaps and clock
 * regressions. The shared activity fixtures hold this and the Python version to identical
 * events.
 */
data class TrackingConfig(
    val minConfidence: Double = 0.5,
    val reacquireS: Double = 30.0,
    val reacquireDistance: Double = 0.15,
    val maxFrameGapS: Double = 5.0,
)

data class FrameObservation(val cameraId: String, val at: Instant, val people: List<PersonObservation>)

data class TrackedPerson(val localId: Int, val observation: PersonObservation, val confident: Boolean) {
    val anchorX get() = observation.anchorX
    val anchorY get() = observation.anchorY
}

data class LostTrack(val localId: Int, val last: PersonObservation, val confident: Boolean)

data class Step(
    val cameraId: String,
    val at: Instant,
    val dt: Double,
    val people: List<TrackedPerson> = emptyList(),
    val appeared: List<Int> = emptyList(),
    val reacquired: List<Int> = emptyList(),
    val lost: List<LostTrack> = emptyList(),
    val afterOutage: Boolean = false,
) {
    val confidentPeople: List<TrackedPerson> get() = people.filter { it.confident }
}

/** Seconds between two instants, computed like Python's `timedelta.total_seconds()`. */
fun secondsBetween(from: Instant, to: Instant): Double = ChronoUnit.MICROS.between(from, to) / 1_000_000.0

class TrackRegistry(private val config: TrackingConfig = TrackingConfig()) {
    private class Lost(val localId: Int, val last: PersonObservation, val lostAt: Instant)

    private var lastAt: Instant? = null
    private var outage = true
    private var nextId = 1
    private var byTrack = mapOf<Int, Int>()
    private var current = linkedMapOf<Int, PersonObservation>()
    private var currentConfident = mapOf<Int, Boolean>()
    private var lost = mutableListOf<Lost>()

    fun cameraLost() {
        outage = true
    }

    fun step(observation: FrameObservation): Step {
        val at = observation.at
        val previous = lastAt
        val dt = if (previous == null || outage) 0.0 else {
            val elapsed = secondsBetween(previous, at)
            if (elapsed > 0 && elapsed <= config.maxFrameGapS) elapsed else 0.0
        }
        val gap = previous != null && !outage && secondsBetween(previous, at) > config.maxFrameGapS
        val afterOutage = outage || gap
        outage = false
        lastAt = at

        lost = lost.filter { val age = secondsBetween(it.lostAt, at); age >= 0 && age <= config.reacquireS }
            .toMutableList()
        val people = mutableListOf<TrackedPerson>()
        val appeared = mutableListOf<Int>()
        val reacquired = mutableListOf<Int>()
        val seen = mutableSetOf<Int>()
        val newByTrack = linkedMapOf<Int, Int>()
        for (obs in observation.people) {
            var localId = byTrack[obs.trackId]
            if (localId == null || localId in seen) {
                localId = reacquire(obs, seen)
                if (localId != null) reacquired += localId
                else {
                    localId = nextId++
                    appeared += localId
                }
            }
            seen += localId
            newByTrack[obs.trackId] = localId
            people += TrackedPerson(localId, obs, obs.confidence >= config.minConfidence)
        }
        val gone = current.filterKeys { it !in seen }.map { (id, obs) ->
            LostTrack(id, obs, currentConfident[id] ?: false)
        }
        for (track in gone) lost += Lost(track.localId, track.last, at)
        byTrack = newByTrack
        current = linkedMapOf<Int, PersonObservation>().apply { people.forEach { put(it.localId, it.observation) } }
        currentConfident = people.associate { it.localId to it.confident }
        return Step(observation.cameraId, at, dt, people, appeared, reacquired, gone, afterOutage)
    }

    private fun reacquire(obs: PersonObservation, taken: Set<Int>): Int? {
        var best: Lost? = null
        var bestDistance = config.reacquireDistance
        for (candidate in lost) {
            if (candidate.localId in taken) continue
            val distance = hypot(obs.anchorX - candidate.last.anchorX, obs.anchorY - candidate.last.anchorY)
            if (distance <= bestDistance) {
                best = candidate
                bestDistance = distance
            }
        }
        best ?: return null
        lost.remove(best)
        return best.localId
    }
}
