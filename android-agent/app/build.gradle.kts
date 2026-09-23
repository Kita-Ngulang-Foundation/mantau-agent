plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "id.mantau.agent"
    compileSdk = 36

    defaultConfig {
        applicationId = "id.mantau.agent"
        minSdk = 26
        targetSdk = 36
        versionCode = 2
        versionName = "0.2.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            // Phones (64- and 32-bit ARM) plus the x86_64 emulator. 32-bit x86
            // only exists on old emulators and would add ~55 MB of native code.
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    buildFeatures {
        buildConfig = true
    }

    androidResources {
        // Models are hashed and handed to the runtimes as-is.
        noCompress += listOf("task", "onnx")
    }
}

dependencies {
    // On-device detection: MediaPipe pose + the ONNX fall-confirmation classifier.
    implementation("com.google.mediapipe:tasks-vision:1.0.0")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.30.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    // JVM build of the same ONNX Runtime API, so unit tests run the real classifier.
    testImplementation("com.microsoft.onnxruntime:onnxruntime:1.30.0")

    // On-device check of the real MediaPipe + ONNX Runtime path (emulator or phone).
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("junit:junit:4.13.2")
}

// The Android ONNX Runtime ships only device native libraries; host unit tests
// use the JVM artifact above instead.
configurations.matching { it.name.startsWith("test") && it.name.endsWith("RuntimeClasspath") }
    .configureEach { exclude(group = "com.microsoft.onnxruntime", module = "onnxruntime-android") }

tasks.withType<Test>().configureEach {
    systemProperty(
        "mantau.contract.fixtures",
        rootProject.file("../../mantau-core/src/mantau_core/contracts/fixtures/v1").absolutePath,
    )
    systemProperty(
        "mantau.core.detection.fixtures",
        rootProject.file("../../mantau-core/src/mantau_core/detection/fixtures").absolutePath,
    )
    // Optional live end-to-end post (SignedFallEventTest); unset = skipped.
    for (name in listOf("server", "agentId", "secret", "cameraId", "seq")) {
        (project.findProperty("mantau.e2e.$name") as String?)?.let { systemProperty("mantau.e2e.$name", it) }
    }
    systemProperty(
        "mantau.protocol.examples",
        rootProject.file("../../protocol/examples").absolutePath,
    )
}
