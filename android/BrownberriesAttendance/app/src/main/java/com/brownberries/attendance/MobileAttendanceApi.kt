package com.brownberries.attendance

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

data class AttendanceSessionInfo(
    val attendanceId: Int,
    val attendanceDate: String,
    val statusLabel: String,
    val checkInAt: String?,
    val checkOutAt: String?,
)

data class BootstrapResponse(
    val token: String = "",
    val userName: String,
    val userEmail: String,
    val shiftStart: String,
    val shiftEnd: String,
    val cafeLat: Double,
    val cafeLng: Double,
    val radiusM: Double,
    val heartbeatIntervalSeconds: Long,
    val offlineGraceMinutes: Long,
    val locationFailureGraceMinutes: Long,
    val activeSession: AttendanceSessionInfo?,
)

class MobileAttendanceApi {
    private val client = OkHttpClient()
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    suspend fun login(
        baseUrl: String,
        email: String,
        password: String,
        deviceId: String,
        deviceName: String,
    ): BootstrapResponse = post(
        url = "${cleanBase(baseUrl)}/api/mobile/attendance/login",
        body = JSONObject()
            .put("email", email)
            .put("password", password)
            .put("device_id", deviceId)
            .put("device_name", deviceName)
            .put("platform", "android")
            .put("app_version", BuildConfig.VERSION_NAME),
    ).let(::parseBootstrap)

    suspend fun bootstrap(baseUrl: String, token: String): BootstrapResponse = get(
        url = "${cleanBase(baseUrl)}/api/mobile/attendance/bootstrap",
        token = token,
    ).let(::parseBootstrap)

    suspend fun checkIn(baseUrl: String, token: String, lat: Double, lng: Double, capturedAtIso: String): AttendanceSessionInfo? =
        post(
            url = "${cleanBase(baseUrl)}/api/mobile/attendance/check-in",
            token = token,
            body = JSONObject()
                .put("lat", lat)
                .put("lng", lng)
                .put("captured_at", capturedAtIso),
        ).optJSONObject("active_session")?.let(::parseSession)

    suspend fun heartbeat(baseUrl: String, token: String, lat: Double, lng: Double, capturedAtIso: String): AttendanceSessionInfo? =
        post(
            url = "${cleanBase(baseUrl)}/api/mobile/attendance/heartbeat",
            token = token,
            body = JSONObject()
                .put("lat", lat)
                .put("lng", lng)
                .put("captured_at", capturedAtIso),
        ).optJSONObject("active_session")?.let(::parseSession)

    suspend fun checkOut(
        baseUrl: String,
        token: String,
        lat: Double?,
        lng: Double?,
        capturedAtIso: String,
        reason: String,
    ): AttendanceSessionInfo? {
        val payload = JSONObject()
            .put("captured_at", capturedAtIso)
            .put("reason", reason)
        if (lat != null) payload.put("lat", lat)
        if (lng != null) payload.put("lng", lng)
        return post(
            url = "${cleanBase(baseUrl)}/api/mobile/attendance/check-out",
            token = token,
            body = payload,
        ).optJSONObject("active_session")?.let(::parseSession)
    }

    suspend fun logout(baseUrl: String, token: String) {
        post(
            url = "${cleanBase(baseUrl)}/api/mobile/attendance/logout",
            token = token,
            body = JSONObject(),
        )
    }

    private suspend fun get(url: String, token: String): JSONObject = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .get()
            .build()
        client.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            val json = JSONObject(body.ifBlank { "{}" })
            if (!response.isSuccessful || !json.optBoolean("ok", false)) {
                throw IllegalStateException(json.optString("message", "Request failed: ${response.code}"))
            }
            json
        }
    }

    private suspend fun post(url: String, body: JSONObject, token: String? = null): JSONObject = withContext(Dispatchers.IO) {
        val requestBuilder = Request.Builder()
            .url(url)
            .post(body.toString().toRequestBody(jsonMediaType))
        if (!token.isNullOrBlank()) {
            requestBuilder.header("Authorization", "Bearer $token")
        }
        client.newCall(requestBuilder.build()).execute().use { response ->
            val bodyText = response.body?.string().orEmpty()
            val json = JSONObject(bodyText.ifBlank { "{}" })
            if (!response.isSuccessful || !json.optBoolean("ok", false)) {
                throw IllegalStateException(json.optString("message", "Request failed: ${response.code}"))
            }
            json
        }
    }

    private fun parseBootstrap(json: JSONObject): BootstrapResponse {
        val user = json.optJSONObject("user") ?: JSONObject()
        val geofence = json.optJSONObject("geofence") ?: JSONObject()
        val policy = json.optJSONObject("policy") ?: JSONObject()
        val shift = json.optJSONObject("shift") ?: JSONObject()
        val activeSession = json.optJSONObject("active_session")?.let(::parseSession)
        return BootstrapResponse(
            token = json.optString("token"),
            userName = user.optString("full_name"),
            userEmail = user.optString("email"),
            shiftStart = shift.optString("shift_start", "09:00"),
            shiftEnd = shift.optString("shift_end", "18:00"),
            cafeLat = geofence.optDouble("cafe_lat", 25.207989477704068),
            cafeLng = geofence.optDouble("cafe_lng", 80.87374457551877),
            radiusM = geofence.optDouble("radius_m", 120.0),
            heartbeatIntervalSeconds = policy.optLong("heartbeat_interval_seconds", 60L),
            offlineGraceMinutes = policy.optLong("offline_checkout_grace_minutes", 60L),
            locationFailureGraceMinutes = policy.optLong("location_failure_grace_minutes", 5L),
            activeSession = activeSession,
        )
    }

    private fun parseSession(json: JSONObject): AttendanceSessionInfo = AttendanceSessionInfo(
        attendanceId = json.optInt("attendance_id", 0),
        attendanceDate = json.optString("attendance_date"),
        statusLabel = json.optString("status_label"),
        checkInAt = json.optString("check_in_at").ifBlank { null },
        checkOutAt = json.optString("check_out_at").ifBlank { null },
    )

    private fun cleanBase(baseUrl: String): String = baseUrl.trim().trimEnd('/')
}
