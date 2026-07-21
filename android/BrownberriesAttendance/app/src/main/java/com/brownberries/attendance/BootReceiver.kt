package com.brownberries.attendance

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != Intent.ACTION_BOOT_COMPLETED) return
        val store = SessionStore(context)
        if (store.monitoringEnabled && store.token.isNotBlank()) {
            LocationMonitorService.start(context)
        }
    }
}
