package id.mantau.agent.inference.fall

import org.json.JSONArray
import org.json.JSONObject
import org.junit.AfterClass
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * The Kotlin fall rules must make exactly the decisions the Python rules recorded in
 * each mantau-core pose-sequence fixture: same per-frame track ids, states and boxes,
 * same fall events, with and without the ONNX confirmation classifier.
 */
class FallParityTest {
    private data class Replay(
        val states: List<List<List<Any>>>,
        val events: List<FallDecision>,
        val calls: List<Pair<FloatArray, Double>>,
    )

    private fun replay(sequence: JSONObject, withClassifier: Boolean): Replay {
        val calls = mutableListOf<Pair<FloatArray, Double>>()
        val confirmer = if (!withClassifier) null else FallConfirmer { features ->
            classifier.confirms(features).also { calls += features.copyOf() to it.second }
        }
        val rules = FallDetector(PoseSequences.config(sequence), confirmer)
        val states = mutableListOf<List<List<Any>>>()
        val events = mutableListOf<FallDecision>()
        for ((t, people) in PoseSequences.frames(sequence)) {
            val (updates, fired) = rules.update(people, t)
            states += updates.map { listOf(it.trackId, it.state.wire, it.bbox[0], it.bbox[1], it.bbox[2], it.bbox[3]) }
            events += fired
        }
        return Replay(states, events, calls)
    }

    private fun expectedStates(expected: JSONObject): List<List<List<Any>>> {
        val frames = expected.getJSONArray("states")
        return List(frames.length()) { i ->
            val tracks = frames.getJSONArray(i)
            List(tracks.length()) { j ->
                val row = tracks.getJSONArray(j)
                listOf(row.getInt(0), row.getString(1), row.getInt(2), row.getInt(3), row.getInt(4), row.getInt(5))
            }
        }
    }

    private fun assertEvents(name: String, expected: JSONArray, actual: List<FallDecision>) {
        assertEquals("$name: event count", expected.length(), actual.size)
        for (i in actual.indices) {
            val e = expected.getJSONObject(i)
            val a = actual[i]
            assertEquals("$name: track", e.getInt("track_id"), a.trackId)
            assertEquals("$name: time", e.getLong("timestamp_ms"), a.timestampMs)
            assertEquals("$name: confidence", e.getDouble("confidence"), a.confidence, 0.0)
            assertEquals("$name: velocity", e.getDouble("velocity"), a.velocity, 1e-9)
            assertEquals("$name: aspect", e.getDouble("aspect_ratio"), a.aspectRatio, 1e-9)
            assertEquals("$name: torso", e.getDouble("torso_angle_deg"), a.torsoAngleDeg, 1e-6)
        }
    }

    @Test
    fun everyFixtureIsCovered() {
        assertTrue(PoseSequences.files.size >= 8)
    }

    @Test
    fun rulesOnlyDecisionsMatchPython() {
        for (file in PoseSequences.files) {
            val sequence = PoseSequences.load(file.nameWithoutExtension)
            val expected = sequence.getJSONObject("expected").getJSONObject("rules_only")
            val actual = replay(sequence, withClassifier = false)
            assertEquals("${file.name}: per-frame track states", expectedStates(expected), actual.states)
            assertEvents(file.name, expected.getJSONArray("events"), actual.events)
        }
    }

    @Test
    fun classifierDecisionsMatchPython() {
        for (file in PoseSequences.files) {
            val sequence = PoseSequences.load(file.nameWithoutExtension)
            val expected = sequence.getJSONObject("expected").getJSONObject("with_classifier")
            val actual = replay(sequence, withClassifier = true)
            assertEquals("${file.name}: per-frame track states", expectedStates(expected), actual.states)
            assertEvents(file.name, expected.getJSONArray("events"), actual.events)

            val calls = expected.getJSONArray("classifier_calls")
            assertEquals("${file.name}: classifier calls", calls.length(), actual.calls.size)
            for (i in actual.calls.indices) {
                val call = calls.getJSONObject(i)
                val features = call.getJSONArray("features")
                val (actualFeatures, actualProb) = actual.calls[i]
                for (k in 0 until features.length()) {
                    val want = features.getDouble(k)
                    assertTrue(
                        "${file.name}: feature ${WindowFeatures.NAMES[k]} ${actualFeatures[k]} != $want",
                        abs(actualFeatures[k] - want) <= 1e-6 * maxOf(1.0, abs(want)),
                    )
                }
                assertEquals("${file.name}: probability", call.getDouble("prob"), actualProb, 1e-6)
            }
        }
    }

    @Test
    fun scenarioOutcomesMatchTheDocumentedBehavior() {
        fun fired(name: String) = replay(PoseSequences.load(name), withClassifier = true).events.size
        assertEquals(1, fired("fall_ybclass_video1"))
        assertEquals(1, fired("fall_urfall_02"))
        assertEquals(0, fired("walk_ybclass_video5"))
        assertEquals(0, fired("slow_liedown_urfall_adl"))
        assertEquals(0, fired("squat_urfall_adl"))
        assertEquals(1, fired("occluded_fall_ybclass_video1"))
        assertEquals(1, fired("reconnect_walk_then_fall"))
    }

    companion object {
        private val classifier by lazy { PoseSequences.classifier() }

        @JvmStatic
        @AfterClass
        fun closeClassifier() = classifier.close()
    }
}
