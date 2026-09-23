package id.mantau.agent.inference.fall

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import org.json.JSONObject
import java.nio.FloatBuffer

/**
 * The fall-confirmation classifier exported from mantau-AI (`fall_classifier.onnx`),
 * run with ONNX Runtime. Same model bytes and sidecar as the Python agents.
 */
class OnnxFallClassifier(modelBytes: ByteArray, sidecarJson: String) : FallConfirmer, AutoCloseable {
    val threshold: Double
    private val inputName: String
    private val environment = OrtEnvironment.getEnvironment()
    private val session: OrtSession

    init {
        val meta = JSONObject(sidecarJson)
        val names = meta.getJSONArray("feature_names").let { a -> List(a.length()) { a.getString(it) } }
        require(names == WindowFeatures.NAMES) { "Classifier feature schema does not match WindowFeatures" }
        threshold = meta.getDouble("threshold")
        inputName = meta.optString("input", "features")
        session = OrtSession.SessionOptions().use { options ->
            options.setIntraOpNumThreads(1)
            environment.createSession(modelBytes, options)
        }
    }

    /** P(real fall) for one feature vector, as a float32 widened to double (as in Python). */
    fun probability(features: FloatArray): Double {
        require(features.size == WindowFeatures.SIZE)
        OnnxTensor.createTensor(environment, FloatBuffer.wrap(features), longArrayOf(1, features.size.toLong()))
            .use { tensor ->
                session.run(mapOf(inputName to tensor)).use { result ->
                    @Suppress("UNCHECKED_CAST")
                    val probabilities = result.get(1).value as Array<FloatArray>
                    return probabilities[0][1].toDouble()
                }
            }
    }

    override fun confirms(features: FloatArray): Pair<Boolean, Double> {
        val p = probability(features)
        return (p >= threshold) to p
    }

    override fun close() = session.close()
}
