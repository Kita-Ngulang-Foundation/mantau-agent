package id.mantau.agent.inference.fall

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/** Bundled models, manifest and fixtures must be exact copies of mantau-core's pinned set. */
class ModelAssetsTest {
    private val assets = File("src/main/assets/mantau")
    private val bundled = ModelArtifacts(File(assets, ModelArtifacts.MANIFEST).readText())
    private val coreFixtures = System.getProperty("mantau.core.detection.fixtures")?.let(::File)

    @Test
    fun everyBundledModelMatchesItsPinnedHash() {
        for (name in listOf(ModelArtifacts.POSE_MODEL, ModelArtifacts.FALL_CLASSIFIER,
                ModelArtifacts.FALL_CLASSIFIER_META, ModelArtifacts.BENCHMARK_FRAME)) {
            bundled.verify(name, File(assets, name).readBytes())
        }
    }

    @Test
    fun tamperedBytesAreRejected() {
        val bytes = File(assets, ModelArtifacts.FALL_CLASSIFIER).readBytes()
        bytes[100] = (bytes[100].toInt() xor 0xFF).toByte()
        assertThrows(ArtifactException::class.java) { bundled.verify(ModelArtifacts.FALL_CLASSIFIER, bytes) }
        assertThrows(ArtifactException::class.java) { bundled.verify("other.bin", ByteArray(1)) }
    }

    @Test
    fun bundledManifestIsMantauCores() {
        assumeTrue(coreFixtures?.isDirectory == true)
        val core = ModelArtifacts(File(coreFixtures, ModelArtifacts.MANIFEST).readText())
        assertEquals(core.pinned, bundled.pinned)
    }

    @Test
    fun poseSequenceCopiesMatchMantauCore() {
        assumeTrue(coreFixtures?.isDirectory == true)
        val core = File(coreFixtures, "pose_sequences").listFiles { f -> f.extension == "json" }!!
            .associate { it.name to JSONObject(it.readText()).toString() }
        val copies = PoseSequences.files.associate { it.name to JSONObject(it.readText()).toString() }
        assertEquals(core.keys, copies.keys)
        for ((name, json) in core) assertEquals("$name differs from mantau-core", json, copies[name])
    }

    @Test
    fun classifierSchemaMatchesTheKotlinFeatures() {
        PoseSequences.classifier().use { classifier ->
            assertEquals(0.8, classifier.threshold, 0.0)
            val p = classifier.probability(FloatArray(WindowFeatures.SIZE))
            assert(p in 0.0..1.0)
        }
    }
}
