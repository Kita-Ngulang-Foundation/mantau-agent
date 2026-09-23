package id.mantau.agent.inference.fall

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.SystemClock
import com.google.mediapipe.framework.image.BitmapImageBuilder
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.vision.core.RunningMode
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarker
import com.google.mediapipe.tasks.vision.poselandmarker.PoseLandmarkerResult
import id.mantau.agent.inference.DetectorAvailability
import id.mantau.agent.inference.FramePerception
import id.mantau.agent.inference.MobileDetector
import id.mantau.agent.uplink.JpegFrame
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * On-device fall detection: MediaPipe Tasks Pose Landmarker, then the Kotlin port of
 * mantau-AI's fall rules with the ONNX confirmation classifier.
 *
 * Every model file is verified against the pinned manifest before loading; any
 * failure leaves the detector unavailable (EDGE is never advertised) with the reason.
 * Pose tracks every frame the inference loop takes while the scene moves; once it has
 * been still for a few seconds, [IdleGate] switches to a stateless image-mode check
 * twice a second, like the Python agent's idle keepalive, so a person who stays
 * still is still observed and an emptied room reads as empty.
 */
class MediaPipeFallDetector(
    private val context: Context,
    private val config: FallConfig = FallConfig(),
) : MobileDetector {
    override val backend = "mediapipe"
    override val availability: DetectorAvailability

    private var poseModel: ByteBuffer? = null
    private var landmarker: PoseLandmarker? = null
    private var snapshotLandmarker: PoseLandmarker? = null
    private val idleGate = IdleGate()
    private var classifier: OnnxFallClassifier? = null
    private var stage: FallRulesStage? = null
    private var benchmarkFrame: ByteArray? = null
    private var lastTimestampMs = Long.MIN_VALUE

    init {
        availability = try {
            val artifacts = ModelArtifacts(asset(ModelArtifacts.MANIFEST).decodeToString())
            val pose = artifacts.verify(ModelArtifacts.POSE_MODEL, asset(ModelArtifacts.POSE_MODEL))
            val onnx = artifacts.verify(ModelArtifacts.FALL_CLASSIFIER, asset(ModelArtifacts.FALL_CLASSIFIER))
            val meta = artifacts.verify(ModelArtifacts.FALL_CLASSIFIER_META, asset(ModelArtifacts.FALL_CLASSIFIER_META))
            benchmarkFrame = artifacts.verify(ModelArtifacts.BENCHMARK_FRAME, asset(ModelArtifacts.BENCHMARK_FRAME))
            poseModel = ByteBuffer.allocateDirect(pose.size).order(ByteOrder.nativeOrder()).put(pose)
                .also { it.rewind() }
            landmarker = createLandmarker(RunningMode.VIDEO)
            classifier = OnnxFallClassifier(onnx, meta.decodeToString()).also {
                stage = FallRulesStage(config, it)
            }
            DetectorAvailability(true, "MediaPipe pose and ONNX fall classifier loaded and verified.", listOf("cpu"))
        } catch (exception: Exception) {
            close()
            DetectorAvailability(false, "EDGE unavailable: detector failed to load (${exception::class.java.simpleName}).")
        }
    }

    /** Mean milliseconds per frame for pose + rules + classifier on a frame with a person. */
    override fun benchmarkLatencyMs(): Double {
        check(availability.available) { availability.reason }
        val bitmap = decode(requireNotNull(benchmarkFrame))
        val probe = createLandmarker(RunningMode.IMAGE)
        try {
            val rules = FallDetector(config)
            val image = BitmapImageBuilder(bitmap).build()
            probe.detect(image) // warm-up
            val runs = 5
            val start = SystemClock.elapsedRealtimeNanos()
            repeat(runs) { i ->
                rules.update(toPoses(probe.detect(image), bitmap.height, bitmap.width), (i + 1) * 200L)
            }
            requireNotNull(classifier).probability(FloatArray(WindowFeatures.SIZE))
            return (SystemClock.elapsedRealtimeNanos() - start) / 1e6 / runs
        } finally {
            probe.close()
        }
    }

    override fun perceive(frame: JpegFrame): FramePerception {
        val skipped = FramePerception(emptyList(), null)
        val landmarker = landmarker ?: return skipped
        val stage = stage ?: return skipped
        // MediaPipe's video mode rejects non-increasing timestamps; skip such frames.
        if (frame.capturedAtMs <= lastTimestampMs) return skipped
        lastTimestampMs = frame.capturedAtMs
        val bitmap = decode(frame.bytes)
        val image = BitmapImageBuilder(bitmap).build()
        val result = when (idleGate.update(luma(bitmap), frame.capturedAtMs)) {
            IdleGate.Pose.TRACK -> landmarker.detectForVideo(image, frame.capturedAtMs)
            IdleGate.Pose.SNAPSHOT -> (snapshotLandmarker ?: createLandmarker(RunningMode.IMAGE)
                .also { snapshotLandmarker = it }).detect(image)
            IdleGate.Pose.SKIP -> return skipped
        }
        return stage.perceive(toPoses(result, bitmap.height, bitmap.width), frame.capturedAtMs,
            bitmap.height, bitmap.width) ?: skipped
    }

    override fun close() {
        landmarker?.close()
        landmarker = null
        snapshotLandmarker?.close()
        snapshotLandmarker = null
        classifier?.close()
        classifier = null
        stage = null
    }

    private fun createLandmarker(mode: RunningMode): PoseLandmarker {
        val options = PoseLandmarker.PoseLandmarkerOptions.builder()
            .setBaseOptions(BaseOptions.builder().setModelAssetBuffer(requireNotNull(poseModel)).build())
            .setRunningMode(mode)
            .setNumPoses(NUM_POSES)
            .setMinPoseDetectionConfidence(MIN_CONFIDENCE)
            .setMinPosePresenceConfidence(MIN_CONFIDENCE)
            .setMinTrackingConfidence(MIN_CONFIDENCE)
            .setOutputSegmentationMasks(false)
            .build()
        return PoseLandmarker.createFromOptions(context, options)
    }

    private fun asset(name: String): ByteArray = context.assets.open("$ASSET_DIR/$name").use { it.readBytes() }

    companion object {
        const val ASSET_DIR = "mantau"
        private const val NUM_POSES = 2
        private const val MIN_CONFIDENCE = 0.5f
        /** Same as the Python agent: wider frames are resized before inference. */
        private const val MAX_WIDTH = 640

        fun decode(jpeg: ByteArray): Bitmap {
            val decoded = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
                ?: throw IllegalArgumentException("Frame is not a decodable JPEG")
            val rgba = if (decoded.config == Bitmap.Config.ARGB_8888) decoded
            else decoded.copy(Bitmap.Config.ARGB_8888, false)
            if (rgba.width <= MAX_WIDTH) return rgba
            val height = (rgba.height * (MAX_WIDTH.toDouble() / rgba.width)).toInt()
            return Bitmap.createScaledBitmap(rgba, MAX_WIDTH, height, true)
        }

        /** Brightness of a small thumbnail, for [IdleGate]. */
        fun luma(bitmap: Bitmap): IntArray {
            val thumb = Bitmap.createScaledBitmap(bitmap, IdleGate.THUMB_WIDTH, IdleGate.THUMB_HEIGHT, true)
            val pixels = IntArray(IdleGate.THUMB_WIDTH * IdleGate.THUMB_HEIGHT)
            thumb.getPixels(pixels, 0, IdleGate.THUMB_WIDTH, 0, 0, IdleGate.THUMB_WIDTH, IdleGate.THUMB_HEIGHT)
            if (thumb !== bitmap) thumb.recycle()
            return IntArray(pixels.size) { i ->
                val p = pixels[i]
                (((p shr 16) and 0xff) * 299 + ((p shr 8) and 0xff) * 587 + (p and 0xff) * 114) / 1000
            }
        }

        fun toPoses(result: PoseLandmarkerResult, height: Int, width: Int): List<PersonPose> =
            result.landmarks().zip(result.worldLandmarks()).map { (image, world) ->
                PersonPose(
                    landmarks = Array(PersonPose.LANDMARKS) { i ->
                        val p = image[i]
                        floatArrayOf(p.x(), p.y(), p.z(), p.visibility().orElse(0f))
                    },
                    world = Array(PersonPose.LANDMARKS) { i ->
                        val p = world[i]
                        floatArrayOf(p.x(), p.y(), p.z())
                    },
                    imageHeight = height,
                    imageWidth = width,
                )
            }
    }
}
