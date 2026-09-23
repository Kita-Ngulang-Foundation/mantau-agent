package id.mantau.agent.activity

import id.mantau.agent.activity.ActivitySettings.ZoneKind
import id.mantau.agent.inference.fall.NumpyMath
import id.mantau.agent.inference.fall.Posture
import id.mantau.agent.uplink.FallEvent
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalTime
import java.time.ZoneId
import kotlin.math.hypot

/**
 * Port of mantau-core `activity.rules`: prolonged position, nocturnal movement and bathroom
 * duration. Same thresholds, same state machines, same deterministic event ids; the shared
 * fixtures in mantau-core `activity/fixtures/activity_sequences` must replay identically.
 */
data class ActivityEvent(
    val eventId: String,
    val cameraId: String,
    val kind: String,
    val severity: String,
    val occurredAt: Instant,
    val confidence: Double,
    val trackId: Int?,
    val zoneId: String?,
    val signals: Map<String, Double>,
)

/** The wire event the uplink sends (same contract as fall events, with kind/severity/zone). */
fun ActivityEvent.toFallEvent() = FallEvent(
    eventId = eventId, cameraId = cameraId, occurredAt = occurredAt, confidence = confidence,
    trackId = trackId, signals = signals, kind = kind, severity = severity, zoneId = zoneId,
)

interface ActivityRule {
    val kind: String
    fun update(step: Step, settings: ActivitySettings): List<ActivityEvent>
    fun reset()
}

object ActivityMath {
    const val CRITICAL_FACTOR = 2.0

    fun eventId(cameraId: String, kind: String, key: String, severity: String): String =
        MessageDigest.getInstance("SHA-256").digest("$cameraId|$kind|$key|$severity".toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }.take(32)

    fun event(
        step: Step, kind: String, severity: String, key: String, signals: Map<String, Double>,
        confidence: Double = 1.0, trackId: Int? = null, zoneId: String? = null,
    ) = ActivityEvent(
        eventId(step.cameraId, kind, key, severity), step.cameraId, kind, severity, step.at,
        confidence.coerceIn(0.0, 1.0), trackId, zoneId, signals,
    )

    fun pointInPolygon(x: Double, y: Double, polygon: List<ActivitySettings.Point>): Boolean {
        var inside = false
        for (i in polygon.indices) {
            val a = polygon[i]
            val b = polygon[(i + 1) % polygon.size]
            if ((a.y > y) != (b.y > y)) {
                val crossing = a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y)
                if (x < crossing) inside = !inside
            }
        }
        return inside
    }

    fun zoneAt(x: Double, y: Double, zones: List<ActivitySettings.Zone>): ActivitySettings.Zone? =
        zones.firstOrNull { pointInPolygon(x, y, it.polygon) }

    fun placedZone(x: Double, y: Double, settings: ActivitySettings) =
        zoneAt(x, y, settings.zones.filter { it.kind != ZoneKind.EXCLUDED })

    fun inWindow(at: Instant, start: LocalTime, end: LocalTime, zone: ZoneId): Boolean {
        val now = at.atZone(zone).toLocalTime()
        return if (start <= end) now >= start && now < end else now >= start || now < end
    }

    fun levels(elapsedS: Double, warningS: Double, fired: MutableSet<String>): List<String> = buildList {
        if (elapsedS >= warningS && fired.add("warning")) add("warning")
        if (elapsedS >= warningS * CRITICAL_FACTOR && fired.add("critical")) add("critical")
    }
}

class ProlongedPositionRule : ActivityRule {
    override val kind = "stillness"

    private class Position(var lastSeen: Instant) {
        var floorStart: Instant? = null
        var floorS = 0.0
        var floorMoved = 0.0
        var floorZone: String? = null
        var floorFired = mutableSetOf<String>()
        var uprightS = 0.0
        var stillStart: Instant? = null
        var stillRefX = 0.0
        var stillRefY = 0.0
        var stillRef = false
        var stillS = 0.0
        var stillMoved = 0.0
        var stillZone: String? = null
        var stillFired = mutableSetOf<String>()
    }

    private val people = linkedMapOf<Int, Position>()

    override fun reset() = people.clear()

    override fun update(step: Step, settings: ActivitySettings): List<ActivityEvent> {
        val events = mutableListOf<ActivityEvent>()
        val night = ActivityMath.inWindow(step.at, settings.nightStart, settings.nightEnd, settings.timezone)
        val hasFloorZones = settings.zones.any { it.kind == ZoneKind.FLOOR }
        for (person in step.people) {
            val state = people.getOrPut(person.localId) { Position(step.at) }
            state.lastSeen = step.at
            if (!person.confident) continue
            events += evaluate(step, person, state, settings.floorMinutes * 60, settings.otherMinutes * 60,
                night, hasFloorZones, settings)
        }
        people.entries.removeIf { secondsBetween(it.value.lastSeen, step.at) > FORGET_S }
        return events
    }

    private fun evaluate(
        step: Step, person: TrackedPerson, state: Position, floorS: Double, otherS: Double,
        night: Boolean, hasFloorZones: Boolean, settings: ActivitySettings,
    ): List<ActivityEvent> {
        val obs = person.observation
        val zone = ActivityMath.placedZone(person.anchorX, person.anchorY, settings)
        val kind = zone?.kind
        val resting = kind == ZoneKind.BED || kind == ZoneKind.SEATING
        val onFloor = obs.posture == Posture.LYING && !resting &&
            (kind == ZoneKind.FLOOR || (kind == null && !hasFloorZones))
        val events = mutableListOf<ActivityEvent>()

        if (onFloor) {
            if (state.floorStart == null) {
                state.floorStart = step.at
                state.floorS = 0.0
                state.floorMoved = 0.0
                state.floorZone = zone?.zoneId
                state.floorFired = mutableSetOf()
            }
            state.floorS += step.dt
            state.floorMoved += obs.motion
            state.uprightS = 0.0
            for (severity in ActivityMath.levels(state.floorS, floorS, state.floorFired)) {
                events += event(step, person, severity, "floor", state.floorStart!!, state.floorS,
                    state.floorMoved, state.floorZone)
            }
        } else if (state.floorStart != null) {
            if (resting) state.floorStart = null
            else if (obs.posture == Posture.STANDING || obs.posture == Posture.SITTING) {
                state.uprightS += step.dt
                if (state.uprightS >= STAND_UP_S) state.floorStart = null
            }
        }

        if (resting || onFloor) {
            state.stillStart = null
            state.stillRef = false
            return events
        }
        if (night) return events
        if (!state.stillRef ||
            hypot(person.anchorX - state.stillRefX, person.anchorY - state.stillRefY) > MOVE_RADIUS) {
            state.stillStart = step.at
            state.stillRef = true
            state.stillRefX = person.anchorX
            state.stillRefY = person.anchorY
            state.stillS = 0.0
            state.stillMoved = 0.0
            state.stillZone = zone?.zoneId
            state.stillFired = mutableSetOf()
            return events
        }
        state.stillS += step.dt
        state.stillMoved += obs.motion
        for (severity in ActivityMath.levels(state.stillS, otherS, state.stillFired)) {
            events += event(step, person, severity, "still", state.stillStart!!, state.stillS,
                state.stillMoved, state.stillZone)
        }
        return events
    }

    private fun event(
        step: Step, person: TrackedPerson, severity: String, branch: String, started: Instant,
        durationS: Double, moved: Double, zoneId: String?,
    ) = ActivityMath.event(
        step, kind, severity, "$branch:${person.localId}@${started.toEpochMilli()}",
        signals = mapOf(
            "duration_s" to NumpyMath.round(durationS, 1),
            "movement" to NumpyMath.round(moved, 4),
            "confidence" to NumpyMath.round(person.observation.confidence, 3),
        ),
        confidence = person.observation.confidence, trackId = person.localId, zoneId = zoneId,
    )

    companion object {
        const val MOVE_RADIUS = 0.05
        const val STAND_UP_S = 3.0
        const val FORGET_S = 600.0
    }
}

class NocturnalMovementRule : ActivityRule {
    override val kind = "nocturnal_movement"

    private class Night(val key: String) {
        var exits = 0
        val fired = mutableSetOf<String>()
        var inBedS = 0.0
        var leavingS = 0.0
        var returningS = 0.0
        var outOfBed = false
        var outStart: Instant? = null
        var outS = 0.0
        var zone: String? = null
        var pendingZone: String? = null
        var pendingS = 0.0
        var transitions = 0
        var bedZone: String? = null
    }

    private var night: Night? = null

    override fun reset() {
        night = null
    }

    override fun update(step: Step, settings: ActivitySettings): List<ActivityEvent> {
        val beds = settings.zones.filter { it.kind == ZoneKind.BED }
        if (beds.isEmpty() || !ActivityMath.inWindow(step.at, settings.nightStart, settings.nightEnd, settings.timezone)) {
            night = null
            return emptyList()
        }
        val key = nightKey(step.at, settings)
        val existing = this.night
        val night = if (existing != null && existing.key == key) existing else Night(key).also { this.night = it }
        val people = step.confidentPeople
        if (people.size != 1) return emptyList()
        val person = people[0]
        val dt = step.dt
        val zone = ActivityMath.placedZone(person.anchorX, person.anchorY, settings)
        val inBed = zone?.kind == ZoneKind.BED
        val zoneKey = zone?.zoneId ?: "-"
        val events = mutableListOf<ActivityEvent>()

        if (inBed) {
            night.inBedS += dt
            night.leavingS = 0.0
            night.bedZone = zone!!.zoneId
            if (night.outOfBed) {
                night.returningS += dt
                if (night.returningS >= RETURN_CONFIRM_S) {
                    night.outOfBed = false
                    night.outStart = null
                    night.outS = 0.0
                    night.inBedS = night.returningS
                }
            }
        } else {
            night.returningS = 0.0
            if (!night.outOfBed) {
                night.leavingS += dt
                if (night.leavingS >= EXIT_CONFIRM_S && night.inBedS >= BED_SETTLE_S) {
                    night.outOfBed = true
                    night.outStart = step.at
                    night.outS = night.leavingS
                    night.inBedS = 0.0
                    night.exits += 1
                    if (night.exits > settings.maxBedExits && night.fired.add("exits")) {
                        events += ActivityMath.event(step, kind, "warning", "exits:${night.key}",
                            mapOf("bed_exits" to night.exits.toDouble()), zoneId = night.bedZone)
                    }
                }
            } else {
                night.outS += dt
                val episode = "out:${night.outStart!!.toEpochMilli()}"
                if (night.outS >= settings.outOfBedMinutes * 60 && night.fired.add(episode)) {
                    events += ActivityMath.event(step, kind, "warning", episode,
                        mapOf("duration_s" to NumpyMath.round(night.outS, 1)))
                }
            }
        }

        if (night.outOfBed) {
            when (zoneKey) {
                night.zone -> {
                    night.pendingZone = null
                    night.pendingS = 0.0
                }
                night.pendingZone -> night.pendingS += dt
                else -> {
                    night.pendingZone = zoneKey
                    night.pendingS = dt
                }
            }
            if (night.pendingZone != null && night.pendingS >= ZONE_SETTLE_S) {
                night.zone = night.pendingZone
                night.pendingZone = null
                night.pendingS = 0.0
                night.transitions += 1
                if (night.transitions >= WANDER_TRANSITIONS && night.fired.add("wander")) {
                    events += ActivityMath.event(step, kind, "warning", "wander:${night.key}",
                        mapOf("transitions" to night.transitions.toDouble()))
                }
            }
        } else {
            night.zone = zoneKey
            night.pendingZone = null
            night.pendingS = 0.0
        }
        return events
    }

    private fun nightKey(at: Instant, settings: ActivitySettings): String {
        val local = at.atZone(settings.timezone)
        var day = local.toLocalDate()
        if (settings.nightStart > settings.nightEnd && local.toLocalTime() < settings.nightEnd) day = day.minusDays(1)
        return day.toString()
    }

    companion object {
        const val BED_SETTLE_S = 60.0
        const val EXIT_CONFIRM_S = 10.0
        const val RETURN_CONFIRM_S = 10.0
        const val ZONE_SETTLE_S = 3.0
        const val WANDER_TRANSITIONS = 8
    }
}

class BathroomDurationRule : ActivityRule {
    override val kind = "bathroom_duration"

    private class Visit(val start: Instant, val zoneId: String) {
        var elapsedS = 0.0
        val fired = mutableSetOf<String>()
    }

    private var visit: Visit? = null

    override fun reset() {
        visit = null
    }

    override fun update(step: Step, settings: ActivitySettings): List<ActivityEvent> {
        val doors = settings.zones.filter { it.kind == ZoneKind.BATHROOM_DOOR }
        if (doors.isEmpty()) {
            visit = null
            return emptyList()
        }
        val current = visit
        if (current != null) {
            val arrived = (step.appeared + step.reacquired).toSet()
            if (step.people.any { it.localId in arrived && ActivityMath.zoneAt(it.anchorX, it.anchorY, doors) != null }) {
                visit = null
                return emptyList()
            }
        }
        if (current == null) {
            if (step.afterOutage) return emptyList()
            for (gone in step.lost) {
                val door = ActivityMath.zoneAt(gone.last.anchorX, gone.last.anchorY, doors)
                if (gone.confident && door != null) {
                    visit = Visit(step.at, door.zoneId)
                    break
                }
            }
            return emptyList()
        }
        if (step.people.isNotEmpty()) return emptyList()
        current.elapsedS += step.dt
        val events = mutableListOf<ActivityEvent>()
        for ((name, limit) in listOf("warning" to settings.bathroomWarningMinutes,
            "critical" to settings.bathroomCriticalMinutes)) {
            if (current.elapsedS >= limit * 60 && current.fired.add(name)) {
                events += ActivityMath.event(step, kind, name, "visit:${current.start.toEpochMilli()}",
                    mapOf("duration_s" to NumpyMath.round(current.elapsedS, 1)), zoneId = current.zoneId)
            }
        }
        return events
    }
}

/**
 * Runs the three rules over each observation with the current settings: same behavior as
 * mantau-core `ActivityEngine` (excluded zones dropped first, disabled rules skipped, a
 * failing rule isolated, rules reset when the settings change).
 */
class ActivityEngine(
    settings: ActivitySettings = ActivitySettings(),
    tracking: TrackingConfig = TrackingConfig(),
    val rules: List<ActivityRule> = listOf(ProlongedPositionRule(), NocturnalMovementRule(), BathroomDurationRule()),
) {
    var settings: ActivitySettings = settings
        private set
    private val registry = TrackRegistry(tracking)
    val failures = linkedMapOf<String, Int>()

    fun applySettings(next: ActivitySettings) {
        if (next != settings) {
            settings = next
            rules.forEach { it.reset() }
        }
    }

    fun cameraLost() = registry.cameraLost()

    @Synchronized
    fun update(observation: FrameObservation): List<ActivityEvent> {
        val excluded = settings.zones.filter { it.kind == ZoneKind.EXCLUDED }
        val filtered = if (excluded.isEmpty()) observation else observation.copy(
            people = observation.people.filter { ActivityMath.zoneAt(it.anchorX, it.anchorY, excluded) == null },
        )
        val step = registry.step(filtered)
        val events = mutableListOf<ActivityEvent>()
        for (rule in rules) {
            if (!enabled(rule.kind)) continue
            try {
                events += rule.update(step, settings)
            } catch (exception: Exception) {
                val name = rule::class.java.simpleName
                failures[name] = (failures[name] ?: 0) + 1
            }
        }
        return events
    }

    private fun enabled(kind: String) = when (kind) {
        "stillness" -> settings.stillnessEnabled
        "nocturnal_movement" -> settings.nocturnalEnabled
        "bathroom_duration" -> settings.bathroomEnabled
        else -> true
    }
}
