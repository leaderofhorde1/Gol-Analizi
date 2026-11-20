plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val runNumber = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
// Kişisel kullanım için imza anahtarı; güncellemelerin aynı imzayla gelmesini sağlar.
val ksPass = System.getenv("KS_PASS") ?: "golsinyali-kisisel"

android {
    namespace = "com.golsinyali.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.golsinyali.app"
        minSdk = 26
        targetSdk = 35
        versionCode = runNumber
        versionName = "1.$runNumber"
    }

    signingConfigs {
        create("release") {
            storeFile = file("release.jks")
            storePassword = ksPass
            keyAlias = "golsinyali"
            keyPassword = ksPass
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.webkit:webkit:1.12.1")
}
