package com.brownberries.attendance

import android.content.Context
import java.util.UUID

class SessionStore(context: Context) {
    private val prefs = context.getSharedPreferences("brownberries_attendance", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = prefs.getString("base_url", "https://brownberriescafe.com") ?: "https://brownberriescafe.com"
        set(value) = prefs.edit().putString("base_url", value.trim()).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value).apply()

    var userName: String
        get() = prefs.getString("user_name", "") ?: ""
        set(value) = prefs.edit().putString("user_name", value).apply()

    var userEmail: String
        get() = prefs.getString("user_email", "") ?: ""
        set(value) = prefs.edit().putString("user_email", value).apply()

    var shiftStart: String
        get() = prefs.getString("shift_start", "09:00") ?: "09:00"
        set(value) = prefs.edit().putString("shift_start", value).apply()

    var shiftEnd: String
        get() = prefs.getString("shift_end", "18:00") ?: "18:00"
        set(value) = prefs.edit().putString("shift_end", value).apply()

    var cafeLat: Double
        get() = java.lang.Double.longBitsToDouble(prefs.getLong("cafe_lat", java.lang.Double.doubleToRawLongBits(25.207989477704068)))
        set(value) = prefs.edit().putLong("cafe_lat", java.lang.Double.doubleToRawLongBits(value)).apply()

    var cafeLng: Double
        get() = java.lang.Double.longBitsToDouble(prefs.getLong("cafe_lng", java.lang.Double.doubleToRawLongBits(80.87374457551877)))
        set(value) = prefs.edit().putLong("cafe_lng", java.lang.Double.doubleToRawLongBits(value)).apply()

    var radiusM: Double
        get() = java.lang.Double.longBitsToDouble(prefs.getLong("radius_m", java.lang.Double.doubleToRawLongBits(120.0)))
        set(value) = prefs.edit().putLong("radius_m", java.lang.Double.doubleToRawLongBits(value)).apply()

    var heartbeatIntervalSeconds: Long
        get() = prefs.getLong("heartbeat_interval_seconds", 60L)
        set(value) = prefs.edit().putLong("heartbeat_interval_seconds", value).apply()

    var offlineGraceMinutes: Long
        get() = prefs.getLong("offline_grace_minutes", 60L)
        set(value) = prefs.edit().putLong("offline_grace_minutes", value).apply()

    var locationFailureGraceMinutes: Long
        get() = prefs.getLong("location_failure_grace_minutes", 5L)
        set(value) = prefs.edit().putLong("location_failure_grace_minutes", value).apply()

    var monitoringEnabled: Boolean
        get() = prefs.getBoolean("monitoring_enabled", false)
        set(value) = prefs.edit().putBoolean("monitoring_enabled", value).apply()

    var checkedIn: Boolean
        get() = prefs.getBoolean("checked_in", false)
        set(value) = prefs.edit().putBoolean("checked_in", value).apply()

    var activeAttendanceId: Int
        get() = prefs.getInt("active_attendance_id", 0)
        set(value) = prefs.edit().putInt("active_attendance_id", value).apply()

    var activeAttendanceDate: String
        get() = prefs.getString("active_attendance_date", "") ?: ""
        set(value) = prefs.edit().putString("active_attendance_date", value).apply()

    var activeStatusLabel: String
        get() = prefs.getString("active_status_label", "") ?: ""
        set(value) = prefs.edit().putString("active_status_label", value).apply()

    var activeCheckInAt: String
        get() = prefs.getString("active_check_in_at", "") ?: ""
        set(value) = prefs.edit().putString("active_check_in_at", value).apply()

    var lastKnownLat: Double
        get() = java.lang.Double.longBitsToDouble(prefs.getLong("last_known_lat", java.lang.Double.doubleToRawLongBits(0.0)))
        set(value) = prefs.edit().putLong("last_known_lat", java.lang.Double.doubleToRawLongBits(value)).apply()

    var lastKnownLng: Double
        get() = java.lang.Double.longBitsToDouble(prefs.getLong("last_known_lng", java.lang.Double.doubleToRawLongBits(0.0)))
        set(value) = prefs.edit().putLong("last_known_lng", java.lang.Double.doubleToRawLongBits(value)).apply()

    var lastDistanceM: Double
        get() = java.lang.Double.longBitsToDouble(prefs.getLong("last_distance_m", java.lang.Double.doubleToRawLongBits(0.0)))
        set(value) = prefs.edit().putLong("last_distance_m", java.lang.Double.doubleToRawLongBits(value)).apply()

    var lastSyncMessage: String
        get() = prefs.getString("last_sync_message", "Idle") ?: "Idle"
        set(value) = prefs.edit().putString("last_sync_message", value).apply()

    var outsideSinceMs: Long
        get() = prefs.getLong("outside_since_ms", 0L)
        set(value) = prefs.edit().putLong("outside_since_ms", value).apply()

    var offlineSinceMs: Long
        get() = prefs.getLong("offline_since_ms", 0L)
        set(value) = prefs.edit().putLong("offline_since_ms", value).apply()

    var locationFailureSinceMs: Long
        get() = prefs.getLong("location_failure_since_ms", 0L)
        set(value) = prefs.edit().putLong("location_failure_since_ms", value).apply()

    var pendingCheckoutAtIso: String
        get() = prefs.getString("pending_checkout_at_iso", "") ?: ""
        set(value) = prefs.edit().putString("pending_checkout_at_iso", value).apply()

    var pendingCheckoutReason: String
        get() = prefs.getString("pending_checkout_reason", "") ?: ""
        set(value) = prefs.edit().putString("pending_checkout_reason", value).apply()

    fun deviceId(): String {
        val existing = prefs.getString("device_id", "") ?: ""
        if (existing.isNotBlank()) return existing
        val generated = UUID.randomUUID().toString()
        prefs.edit().putString("device_id", generated).apply()
        return generated
    }

    fun clearAuth() {
        token = ""
        userName = ""
        userEmail = ""
        checkedIn = false
        activeAttendanceId = 0
        activeAttendanceDate = ""
        activeStatusLabel = ""
        activeCheckInAt = ""
        monitoringEnabled = false
        pendingCheckoutAtIso = ""
        pendingCheckoutReason = ""
    }

    fun applyBootstrap(bootstrap: BootstrapResponse) {
        userName = bootstrap.userName
        userEmail = bootstrap.userEmail
        shiftStart = bootstrap.shiftStart
        shiftEnd = bootstrap.shiftEnd
        cafeLat = bootstrap.cafeLat
        cafeLng = bootstrap.cafeLng
        radiusM = bootstrap.radiusM
        heartbeatIntervalSeconds = bootstrap.heartbeatIntervalSeconds
        offlineGraceMinutes = bootstrap.offlineGraceMinutes
        locationFailureGraceMinutes = bootstrap.locationFailureGraceMinutes
        applyAttendanceState(bootstrap.activeSession)
    }

    fun applyAttendanceState(session: AttendanceSessionInfo?) {
        checkedIn = session != null && session.checkOutAt.isNullOrBlank()
        activeAttendanceId = session?.attendanceId ?: 0
        activeAttendanceDate = session?.attendanceDate ?: ""
        activeStatusLabel = session?.statusLabel ?: ""
        activeCheckInAt = session?.checkInAt ?: ""
    }
}
