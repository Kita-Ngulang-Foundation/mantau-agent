package id.mantau.agent.inference.fall

import org.json.JSONObject
import java.security.MessageDigest

/**
 * Pinned model files, from the same `model_artifacts.json` manifest mantau-core uses
 * for the Python agent. A file is only handed to a runtime after its size and SHA-256
 * match; the verified bytes are what gets loaded (no second read after the check).
 */
class ModelArtifacts(manifestJson: String) {
    data class Pinned(val sha256: String, val bytes: Long)

    val pinned: Map<String, Pinned> = JSONObject(manifestJson).getJSONObject("artifacts").let { all ->
        all.keys().asSequence().associateWith { name ->
            all.getJSONObject(name).let { Pinned(it.getString("sha256"), it.getLong("bytes")) }
        }
    }

    fun verify(name: String, content: ByteArray): ByteArray {
        val expected = pinned[name] ?: throw ArtifactException("$name is not a pinned model artifact")
        if (content.size.toLong() != expected.bytes || sha256(content) != expected.sha256) {
            throw ArtifactException("model artifact $name does not match its pinned SHA-256")
        }
        return content
    }

    companion object {
        const val POSE_MODEL = "pose_landmarker_lite.task"
        const val FALL_CLASSIFIER = "fall_classifier.onnx"
        const val FALL_CLASSIFIER_META = "fall_classifier.onnx.json"
        const val BENCHMARK_FRAME = "benchmark_person.jpg"
        const val MANIFEST = "model_artifacts.json"

        fun sha256(content: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(content).joinToString("") { "%02x".format(it) }
    }
}

class ArtifactException(message: String) : RuntimeException(message)
