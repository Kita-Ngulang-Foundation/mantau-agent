package id.mantau.agent.inference

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import id.mantau.agent.rtsp.EncodedFrame
import id.mantau.agent.rtsp.VideoFormat
import id.mantau.agent.uplink.JpegFrame
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer

interface FrameDecoder : AutoCloseable {
    fun configure(format: VideoFormat)
    fun decode(frame: EncodedFrame): JpegFrame?
}

class AndroidH264JpegDecoder(
    private val maxWidth: Int,
    private val quality: Int,
) : FrameDecoder {
    private var codec: MediaCodec? = null
    private var configured: VideoFormat? = null

    override fun configure(format: VideoFormat) {
        if (configured == format) return
        close()
        val mediaFormat = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, format.width, format.height)
        mediaFormat.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
        format.sps?.let { mediaFormat.setByteBuffer("csd-0", ByteBuffer.wrap(it)) }
        format.pps?.let { mediaFormat.setByteBuffer("csd-1", ByteBuffer.wrap(it)) }
        codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).also {
            it.configure(mediaFormat, null, null, 0)
            it.start()
        }
        configured = format
    }

    override fun decode(frame: EncodedFrame): JpegFrame? {
        val decoder = codec ?: return null
        val inputIndex = decoder.dequeueInputBuffer(0)
        if (inputIndex < 0) return null
        val input = decoder.getInputBuffer(inputIndex)
        if (input == null || frame.bytes.size > input.capacity()) {
            decoder.queueInputBuffer(inputIndex, 0, 0, frame.capturedAt.toEpochMilli() * 1_000, 0)
            return null
        }
        input.clear()
        input.put(frame.bytes)
        decoder.queueInputBuffer(inputIndex, 0, frame.bytes.size, frame.capturedAt.toEpochMilli() * 1_000, 0)

        val info = MediaCodec.BufferInfo()
        repeat(3) {
            val outputIndex = decoder.dequeueOutputBuffer(info, 0)
            if (outputIndex >= 0) {
                val image = decoder.getOutputImage(outputIndex)
                val jpeg = image?.use(::encode)
                decoder.releaseOutputBuffer(outputIndex, false)
                if (jpeg != null) return JpegFrame(
                    jpeg.bytes, jpeg.width, jpeg.height, info.presentationTimeUs / 1_000,
                )
            } else if (outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                // Dimensions/crop are read from each output Image.
            } else return null
        }
        return null
    }

    private data class EncodedJpeg(val bytes: ByteArray, val width: Int, val height: Int)

    private fun encode(image: Image): EncodedJpeg? {
        if (image.format != ImageFormat.YUV_420_888) return null
        val crop = image.cropRect
        val nv21 = yuv420ToNv21(image, crop)
        val full = ByteArrayOutputStream()
        if (!YuvImage(nv21, ImageFormat.NV21, crop.width(), crop.height(), null)
                .compressToJpeg(Rect(0, 0, crop.width(), crop.height()), quality, full)) return null
        if (crop.width() <= maxWidth) return EncodedJpeg(full.toByteArray(), crop.width(), crop.height())

        val source = BitmapFactory.decodeByteArray(full.toByteArray(), 0, full.size()) ?: return null
        return try {
            val height = (source.height * (maxWidth.toDouble() / source.width)).toInt().coerceAtLeast(1)
            val scaled = Bitmap.createScaledBitmap(source, maxWidth, height, true)
            try {
                val output = ByteArrayOutputStream()
                if (!scaled.compress(Bitmap.CompressFormat.JPEG, quality, output)) null
                else EncodedJpeg(output.toByteArray(), maxWidth, height)
            } finally {
                if (scaled !== source) scaled.recycle()
            }
        } finally {
            source.recycle()
        }
    }

    private fun yuv420ToNv21(image: Image, crop: Rect): ByteArray {
        val width = crop.width()
        val height = crop.height()
        val output = ByteArray(width * height * 3 / 2)
        copyPlane(image.planes[0], crop.left, crop.top, width, height, output, 0, 1)
        val chromaLeft = crop.left / 2
        val chromaTop = crop.top / 2
        val chromaWidth = width / 2
        val chromaHeight = height / 2
        val chromaOffset = width * height
        copyPlane(image.planes[2], chromaLeft, chromaTop, chromaWidth, chromaHeight, output, chromaOffset, 2)
        copyPlane(image.planes[1], chromaLeft, chromaTop, chromaWidth, chromaHeight, output, chromaOffset + 1, 2)
        return output
    }

    private fun copyPlane(
        plane: Image.Plane,
        left: Int,
        top: Int,
        width: Int,
        height: Int,
        output: ByteArray,
        outputOffset: Int,
        outputStride: Int,
    ) {
        val buffer = plane.buffer
        val bufferStart = buffer.position()
        var target = outputOffset
        for (row in 0 until height) {
            val rowStart = bufferStart + (top + row) * plane.rowStride + left * plane.pixelStride
            for (column in 0 until width) {
                output[target] = buffer.get(rowStart + column * plane.pixelStride)
                target += outputStride
            }
        }
    }

    override fun close() {
        codec?.let { runCatching { it.stop() }; runCatching { it.release() } }
        codec = null
        configured = null
    }
}
