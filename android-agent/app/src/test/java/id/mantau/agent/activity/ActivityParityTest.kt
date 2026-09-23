package id.mantau.agent.activity

import id.mantau.agent.inference.fall.PersonObservation
import id.mantau.agent.inference.fall.Posture
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.time.Instant
import java.time.format.DateTimeFormatterBuilder

/**
 * The Kotlin activity rules must produce exactly the events mantau-core's Python rules
 * recorded for every scenario fixture: same ids, kinds, severities, times, confidences,
 * track and zone ids, and signal values.
 */
class ActivityParityTest {
    private val directory = File(requireNotNull(javaClass.classLoader.getResource("activity_sequences")).toURI())
    private val files = directory.listFiles { f -> f.extension == "json" }!!.sortedBy { it.name }
    private val iso = DateTimeFormatterBuilder().appendInstant(3).toFormatter()

    private fun replay(fixture: JSONObject): List<JSONObject> {
        val engine = ActivityEngine(ActivitySettings.parse(fixture.getJSONObject("settings")))
        val cameraId = fixture.getString("camera_id")
        val steps = fixture.getJSONArray("steps")
        val out = mutableListOf<JSONObject>()
        for (i in 0 until steps.length()) {
            val step = steps.getJSONObject(i)
            if (step.optBoolean("camera_lost", false)) {
                engine.cameraLost()
                continue
            }
            val people = step.getJSONArray("people").let { array ->
                List(array.length()) { p ->
                    val row = array.getJSONArray(p)
                    PersonObservation(
                        trackId = row.getInt(0), left = row.getDouble(1), top = row.getDouble(2),
                        width = row.getDouble(3), height = row.getDouble(4),
                        posture = Posture.of(row.getString(5)), motion = row.getDouble(6),
                        confidence = row.getDouble(7),
                    )
                }
            }
            for (event in engine.update(FrameObservation(cameraId, Instant.parse(step.getString("at")), people))) {
                out += JSONObject()
                    .put("event_id", event.eventId).put("kind", event.kind).put("severity", event.severity)
                    .put("occurred_at", iso.format(event.occurredAt)).put("confidence", event.confidence)
                    .put("track_id", event.trackId ?: JSONObject.NULL).put("zone_id", event.zoneId ?: JSONObject.NULL)
                    .put("signals", JSONObject(event.signals))
            }
        }
        return out
    }

    private fun same(expected: JSONObject, actual: JSONObject, where: String) {
        for (key in listOf("event_id", "kind", "severity", "occurred_at")) {
            assertEquals("$where $key", expected.getString(key), actual.getString(key))
        }
        assertEquals("$where confidence", expected.getDouble("confidence"), actual.getDouble("confidence"), 0.0)
        assertEquals("$where track_id", expected.opt("track_id").toString(), actual.opt("track_id").toString())
        assertEquals("$where zone_id", expected.opt("zone_id").toString(), actual.opt("zone_id").toString())
        val want = expected.getJSONObject("signals")
        val got = actual.getJSONObject("signals")
        assertEquals("$where signals", want.keySet(), got.keySet())
        for (key in want.keySet()) assertEquals("$where $key", want.getDouble(key), got.getDouble(key), 0.0)
    }

    @Test
    fun everyScenarioMatchesPythonExactly() {
        assertTrue(files.size >= 20)
        var events = 0
        for (file in files) {
            val fixture = JSONObject(file.readText())
            val expected: JSONArray = fixture.getJSONArray("expected")
            val actual = replay(fixture)
            assertEquals("${file.name}: event count", expected.length(), actual.size)
            for (i in actual.indices) same(expected.getJSONObject(i), actual[i], "${file.name}[$i]")
            events += actual.size
        }
        assertTrue(events >= 15)
    }

    @Test
    fun fixtureCopiesMatchMantauCore() {
        val core = System.getProperty("mantau.core.activity.fixtures")?.let(::File)
        assumeTrue(core?.isDirectory == true)
        val theirs = core!!.listFiles { f -> f.extension == "json" }!!.associate { it.name to JSONObject(it.readText()).toString() }
        val ours = files.associate { it.name to JSONObject(it.readText()).toString() }
        assertEquals(theirs, ours)
    }
}
