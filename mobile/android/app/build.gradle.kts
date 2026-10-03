plugins { id("com.android.application"); id("org.jetbrains.kotlin.android") }
android {
    namespace = "com.pencilfinely.expmonitor"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.pencilfinely.expmonitor"
        minSdk = 28
        targetSdk = 35
        versionCode = 3
        versionName = "0.5.2"
    }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }
}
dependencies {
    implementation("androidx.core:core:1.15.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
