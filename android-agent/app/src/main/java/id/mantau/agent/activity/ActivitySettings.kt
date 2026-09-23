package id.mantau.agent.activity

import org.json.JSONObject
import java.time.LocalTime
import java.time.ZoneId

/**
 * The household's detection settings, parsed from the same JSON the server sends in
 * `apply_detection_settings` (mantau-core `contracts.detection.DetectionSettings`, validated
 * server-side). Only what the activity rules read is kept.
 */
data class ActivitySettings(
    val version: Int = 1,
    val timezone: ZoneId = ZoneId.of("Asia/Jakarta"),
    val zones: List<Zone> = emptyList(),
    val stillnessEnabled: Boolean = true,
    val floorMinutes: Double = 2.0,
    val otherMinutes: Double = 45.0,
    val nocturnalEnabled: Boolean = true,
    val nightStart: LocalTime = LocalTime.of(22, 0),
    val nightEnd: LocalTime = LocalTime.of(5, 0),
    val outOfBedMinutes: Double = 20.0,
    val maxBedExits: Int = 3,
    val bathroomEnabled: Boolean = true,
    val bathroomWarningMinutes: Double = 20.0,
    val bathroomCriticalMinutes: Double = 40.0,
) {
    enum class ZoneKind(val wire: String) {
        BED("bed"), BATHROOM_DOOR("bathroom_door"), FLOOR("floor"), SEATING("seating"), EXCLUDED("excluded");

        companion object {
            fun of(wire: String) = entries.first { it.wire == wire }
        }
    }

    data class Point(val x: Double, val y: Double)
    data class Zone(val zoneId: String, val kind: ZoneKind, val polygon: List<Point>)

    companion object {
        fun parse(json: JSONObject): ActivitySettings {
            val zones = json.optJSONArray("zones")?.let { array ->
                List(array.length()) { i ->
                    val zone = array.getJSONObject(i)
                    val points = zone.getJSONArray("polygon")
                    Zone(
                        zoneId = zone.getString("zone_id"),
                        kind = ZoneKind.of(zone.getString("kind")),
                        polygon = List(points.length()) { p ->
                            points.getJSONObject(p).let { Point(it.getDouble("x"), it.getDouble("y")) }
                        },
                    )
                }
            }.orEmpty()
            val defaults = ActivitySettings()
            val stillness = json.optJSONObject("stillness") ?: JSONObject()
            val nocturnal = json.optJSONObject("nocturnal") ?: JSONObject()
            val bathroom = json.optJSONObject("bathroom") ?: JSONObject()
            return ActivitySettings(
                version = json.optInt("version", 1),
                timezone = ZoneId.of(json.optString("timezone", "Asia/Jakarta")),
                zones = zones,
                stillnessEnabled = stillness.optBoolean("enabled", true),
                floorMinutes = stillness.optDouble("floor_minutes", defaults.floorMinutes),
                otherMinutes = stillness.optDouble("other_minutes", defaults.otherMinutes),
                nocturnalEnabled = nocturnal.optBoolean("enabled", true),
                nightStart = nocturnal.optString("start").takeIf { it.isNotEmpty() }?.let(LocalTime::parse)
                    ?: defaults.nightStart,
                nightEnd = nocturnal.optString("end").takeIf { it.isNotEmpty() }?.let(LocalTime::parse)
                    ?: defaults.nightEnd,
                outOfBedMinutes = nocturnal.optDouble("out_of_bed_minutes", defaults.outOfBedMinutes),
                maxBedExits = nocturnal.optInt("max_bed_exits", defaults.maxBedExits),
                bathroomEnabled = bathroom.optBoolean("enabled", true),
                bathroomWarningMinutes = bathroom.optDouble("warning_minutes", defaults.bathroomWarningMinutes),
                bathroomCriticalMinutes = bathroom.optDouble("critical_minutes", defaults.bathroomCriticalMinutes),
            )
        }
    }
}
