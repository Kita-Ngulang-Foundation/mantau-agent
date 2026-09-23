package id.mantau.agent.inference

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

class InferencePolicyTest {
    private val cool = ThermalStateProvider { ThermalState.NONE }

    private fun policy(
        edge: Boolean, cloud: Boolean, fast: Boolean = true, failure: String? = null,
        thermal: ThermalStateProvider = cool,
    ) = InferenceModePolicy(
        detectorAvailable = { edge }, detectorFailure = { failure },
        cloudAvailable = { cloud }, detectorFastEnough = { fast }, thermal = thermal,
    )

    @Test
    fun `AUTO and detector failure fall back to server inference`() {
        val selected = policy(edge = false, cloud = true, failure = "verified model artifact is missing")
            .select("AUTO")
        assertEquals("CLOUD", selected.effective)
        assertTrue(selected.reason.contains("model"))
    }

    @Test
    fun `explicit EDGE with a broken model falls back to CLOUD instead of running nothing`() {
        val selected = policy(edge = false, cloud = true, failure = "detector failed to load").select("EDGE")
        assertEquals("CLOUD", selected.effective)
        assertTrue(selected.reason.contains("falling back to CLOUD"))
    }

    @Test
    fun `AUTO moves a too-slow detector to CLOUD but keeps it when CLOUD is missing`() {
        assertEquals("CLOUD", policy(edge = true, cloud = true, fast = false).select("AUTO").effective)
        assertEquals("EDGE", policy(edge = true, cloud = false, fast = false).select("AUTO").effective)
        assertEquals("EDGE", policy(edge = true, cloud = true, fast = true).select("AUTO").effective)
    }

    @Test
    fun `CLOUD and HYBRID fall back to EDGE when the server has no inference`() {
        assertEquals("EDGE", policy(edge = true, cloud = false).select("CLOUD").effective)
        assertEquals("EDGE", policy(edge = true, cloud = false).select("HYBRID").effective)
        assertEquals("CLOUD", policy(edge = false, cloud = true).select("HYBRID").effective)
        assertEquals("HYBRID", policy(edge = true, cloud = true).select("HYBRID").effective)
        assertEquals("CLOUD", policy(edge = true, cloud = true).select("CLOUD").effective)
    }

    @Test
    fun `nothing available is an explicit failure`() {
        assertThrows(UnsupportedOperationException::class.java) {
            policy(edge = false, cloud = false, failure = "no model").select("AUTO")
        }
    }

    @Test
    fun `thermal pressure moves edge to cloud only when cloud exists`() {
        var thermal = ThermalState.NONE
        val provider = ThermalStateProvider { thermal }
        assertEquals("EDGE", policy(edge = true, cloud = true, thermal = provider).select("AUTO").effective)
        thermal = ThermalState.SEVERE
        val selected = policy(edge = true, cloud = true, thermal = provider).select("EDGE")
        assertEquals("CLOUD", selected.effective)
        assertTrue(selected.reason.contains("Thermal severe"))
        assertEquals("EDGE", policy(edge = true, cloud = false, thermal = provider).select("EDGE").effective)
    }

    @Test
    fun `hybrid confirmations are rate limited per event`() {
        val confirmations = HybridConfirmationPolicy(minIntervalMs = 5_000)
        assertTrue(confirmations.shouldUpload("event-1", 0))
        assertFalse(confirmations.shouldUpload("event-1", 10_000))
        assertFalse(confirmations.shouldUpload("event-2", 4_999))
        assertTrue(confirmations.shouldUpload("event-2", 5_000))
    }

    @Test
    fun `capability report lists only modes that can run`() {
        val facts = CapabilityFacts(
            architecture = "arm64-v8a", memoryBytes = 3L shl 30, gpu = null, nnapiAvailable = true,
            delegates = emptyList(), detectorBackend = "mediapipe", detectorAvailable = true,
            detectorReason = "", inferenceLatencyMs = 40.0,
        )
        assertEquals(listOf("AUTO", "EDGE"), facts.snapshot(ThermalState.NONE, false).supportedModes)
        assertEquals(listOf("AUTO", "EDGE", "CLOUD", "HYBRID"), facts.snapshot(ThermalState.NONE, true).supportedModes)
        assertEquals("EDGE", facts.snapshot(ThermalState.NONE, true).recommendedMode)

        val slow = facts.copy(inferenceLatencyMs = 250.0)
        assertFalse(slow.detectorFastEnough)
        assertEquals("CLOUD", slow.snapshot(ThermalState.NONE, true).recommendedMode)
        assertEquals("EDGE", slow.snapshot(ThermalState.NONE, false).recommendedMode)

        val broken = facts.copy(detectorAvailable = false, detectorReason = "EDGE unavailable: detector failed to load.")
        assertEquals(listOf("AUTO", "CLOUD"), broken.snapshot(ThermalState.NONE, true).supportedModes)
        assertEquals("CLOUD", broken.snapshot(ThermalState.NONE, true).recommendedMode)
        assertEquals("AUTO", broken.snapshot(ThermalState.NONE, false).recommendedMode)
    }

    @Test
    fun `fall gate applies cooldown and track dedupe without synthetic events`() {
        val gate = FallEventGate("cam", cooldownMs = 1_000, dedupeMs = 5_000)
        val candidate = DetectionCandidate(.91, trackId = 7, signals = mapOf("velocity" to .5))
        assertNotNull(gate.accept(candidate, Instant.ofEpochMilli(0)))
        assertNull(gate.accept(candidate, Instant.ofEpochMilli(999)))
        assertNull(gate.accept(candidate, Instant.ofEpochMilli(1_001)))
        assertNotNull(gate.accept(candidate, Instant.ofEpochMilli(5_001)))
    }
}
