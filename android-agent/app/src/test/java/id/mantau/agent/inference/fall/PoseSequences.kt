package id.mantau.agent.inference.fall

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Loads the recorded pose-sequence fixtures (copied from mantau-core) for unit tests. */
object PoseSequences {
    private val directory: File = File(
        requireNotNull(PoseSequences::class.java.classLoader.getResource("pose_sequences")) {
            "pose_sequences test resources missing"
        }.toURI(),
    )

    val files: List<File> get() = directory.listFiles { f -> f.extension == "json" }!!.sortedBy { it.name }

    fun load(name: String): JSONObject = JSONObject(File(directory, "$name.json").readText())

    fun config(sequence: JSONObject): FallConfig = sequence.getJSONObject("fall_config").let {
        FallConfig(
            windowSeconds = it.getDouble("window_seconds"),
            dropVelocity = it.getDouble("drop_velocity"),
            aspectRatioHorizontal = it.getDouble("aspect_ratio_horizontal"),
            torsoAngleHorizontal = it.getDouble("torso_angle_horizontal"),
            timeOnGround = it.getDouble("time_on_ground"),
            trackMatchDistance = it.getDouble("track_match_distance"),
            trackMaxAge = it.getDouble("track_max_age"),
            centroidFallenY = it.getDouble("centroid_fallen_y"),
            headHeightFallen = it.getDouble("head_height_fallen"),
        )
    }

    /** (timestamp ms, poses) for every recorded frame. */
    fun frames(sequence: JSONObject): List<Pair<Long, List<PersonPose>>> {
        val shape = sequence.getJSONArray("image_shape")
        val height = shape.getInt(0)
        val width = shape.getInt(1)
        val frames = sequence.getJSONArray("frames")
        return List(frames.length()) { i ->
            val frame = frames.getJSONObject(i)
            val people = frame.getJSONArray("people")
            frame.getLong("t") to List(people.length()) { p ->
                val person = people.getJSONObject(p)
                PersonPose(
                    landmarks = rows(person.getJSONArray("lm")).map { floatArrayOf(it[0], it[1], 0f, it[2]) }
                        .toTypedArray(),
                    world = rows(person.getJSONArray("world")).toTypedArray(),
                    imageHeight = height,
                    imageWidth = width,
                )
            }
        }
    }

    private fun rows(array: JSONArray): List<FloatArray> = List(array.length()) { i ->
        val row = array.getJSONArray(i)
        FloatArray(row.length()) { row.getDouble(it).toFloat() }
    }

    fun classifier(): OnnxFallClassifier {
        val assets = File("src/main/assets/mantau")
        return OnnxFallClassifier(
            File(assets, ModelArtifacts.FALL_CLASSIFIER).readBytes(),
            File(assets, ModelArtifacts.FALL_CLASSIFIER_META).readText(),
        )
    }
}
