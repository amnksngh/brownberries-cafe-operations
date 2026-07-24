package com.brownberries.attendance

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.brownberries.attendance.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URL
import java.time.ZoneId
import java.time.ZonedDateTime

class MainActivity : AppCompatActivity() {
    private val cocoa = Color.rgb(111, 74, 53)
    private val darkCocoa = Color.rgb(79, 48, 34)
    private val ink = Color.rgb(47, 30, 22)
    private val mutedInk = Color.rgb(122, 90, 72)
    private val cream = Color.rgb(255, 249, 244)
    private val border = Color.rgb(230, 215, 202)
    private val teal = Color.rgb(31, 107, 104)
    private lateinit var binding: ActivityMainBinding
    private lateinit var store: SessionStore
    private val api = MobileAttendanceApi()
    private var workspaceJson: JSONObject? = null
    private var activeTab = "profile"
    private var pendingDocumentUri: Uri? = null
    private var backgroundPermissionPrompted = false
    private lateinit var permissionLauncher: ActivityResultLauncher<Array<String>>

    private data class StaffCartLine(
        val item: JSONObject,
        var quantity: Int,
        var sizeLabel: String,
        var unitPrice: Double,
        var parcel: Boolean,
    )

    private val documentPicker = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        pendingDocumentUri = uri
        toast(if (uri == null) "No document selected" else "Document selected. Tap Upload Document to send it.")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        store = SessionStore(this)

        permissionLauncher = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            if (hasCorePermissions()) {
                if (needsBackgroundPermission() && !backgroundPermissionPrompted) {
                    backgroundPermissionPrompted = true
                    permissionLauncher.launch(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION))
                } else {
                    startAutomaticMonitoring()
                    renderState()
                }
            } else {
                toast("Location permission is required for automatic attendance.")
            }
        }

        binding.loginButton.setOnClickListener { doLogin() }
        binding.logoutNavButton.setOnClickListener { doLogout() }
        binding.profileTabButton.setOnClickListener { showTab("profile") }
        binding.tableOrderingTabButton.setOnClickListener { showTab("table") }
        binding.availabilityTabButton.setOnClickListener { showTab("availability") }

        renderState()
        if (store.token.isNotBlank()) {
            ensurePermissionsAndStartMonitoring()
            refreshWorkspace(silent = true)
        }
    }

    override fun onResume() {
        super.onResume()
        if (::store.isInitialized && store.token.isNotBlank()) {
            startAutomaticMonitoringIfPossible()
            LocationMonitorService.refreshNow(this)
        }
    }

    private fun doLogin() {
        val email = binding.emailInput.text?.toString()?.trim().orEmpty()
        val password = binding.passwordInput.text?.toString().orEmpty()
        if (email.isBlank() || password.isBlank()) {
            toast("Email and password are required.")
            return
        }
        binding.loginButton.isEnabled = false
        lifecycleScope.launch {
            try {
                val bootstrap = api.login(
                    store.baseUrl,
                    email,
                    password,
                    store.deviceId(),
                    "${Build.MANUFACTURER} ${Build.MODEL}",
                )
                store.token = bootstrap.token
                store.applyBootstrap(bootstrap)
                store.monitoringEnabled = true
                binding.loginErrorText.visibility = View.GONE
                renderState()
                ensurePermissionsAndStartMonitoring()
                refreshWorkspace(silent = true)
                toast("Welcome, ${bootstrap.userName}")
            } catch (t: Throwable) {
                val message = t.message ?: "Sign-in failed"
                binding.loginErrorText.text = message
                binding.loginErrorText.visibility = View.VISIBLE
                toast(message)
            } finally {
                binding.loginButton.isEnabled = true
            }
        }
    }

    private fun refreshWorkspace(silent: Boolean = false) {
        if (store.token.isBlank()) return
        if (!silent) showLoading("Refreshing live workspace…")
        lifecycleScope.launch {
            try {
                workspaceJson = api.workspace(store.baseUrl, store.token)
                renderState()
                renderCurrentTab()
                if (!silent) toast("Updated from brownberriescafe.com")
            } catch (t: Throwable) {
                if (workspaceJson == null) showError(t.message ?: "Workspace unavailable")
                if (!silent) toast(t.message ?: "Workspace refresh failed")
            }
        }
    }

    private fun refreshBootstrap(silent: Boolean = false) {
        lifecycleScope.launch {
            try {
                store.applyBootstrap(api.bootstrap(store.baseUrl, store.token))
                store.monitoringEnabled = true
                ensurePermissionsAndStartMonitoring()
                renderState()
                if (!silent) toast("Attendance settings refreshed")
            } catch (t: Throwable) {
                if (!silent) toast(t.message ?: "Refresh failed")
            }
        }
    }

    private fun checkOut() {
        if (store.token.isBlank() || !store.checkedIn) {
            toast("There is no active attendance session.")
            return
        }
        lifecycleScope.launch {
            try {
                val session = api.checkOut(
                    store.baseUrl,
                    store.token,
                    store.lastKnownLat.takeIf { it != 0.0 },
                    store.lastKnownLng.takeIf { it != 0.0 },
                    nowIso(),
                    "manual",
                )
                store.applyAttendanceState(session)
                refreshWorkspace(silent = true)
                toast("Checked out")
            } catch (t: Throwable) {
                toast(t.message ?: "Check-Out failed")
            }
        }
    }

    private fun doLogout() {
        lifecycleScope.launch {
            runCatching { if (store.token.isNotBlank()) api.logout(store.baseUrl, store.token) }
            LocationMonitorService.stop(this@MainActivity)
            store.clearAuth()
            workspaceJson = null
            binding.emailInput.text?.clear()
            binding.passwordInput.text?.clear()
            binding.loginErrorText.visibility = View.GONE
            renderState()
            toast("Signed out")
        }
    }

    private fun ensurePermissionsAndStartMonitoring() {
        if (hasCorePermissions()) {
            startAutomaticMonitoring()
            return
        }
        val permissions = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) permissions += Manifest.permission.POST_NOTIFICATIONS
        permissionLauncher.launch(permissions.toTypedArray())
    }

    private fun needsBackgroundPermission(): Boolean = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION) != PackageManager.PERMISSION_GRANTED

    private fun startAutomaticMonitoring() {
        store.monitoringEnabled = true
        LocationMonitorService.start(this)
    }

    private fun startAutomaticMonitoringIfPossible() {
        if (hasCorePermissions()) startAutomaticMonitoring()
    }

    private fun hasCorePermissions(): Boolean {
        val fine = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        return fine || coarse
    }

    private fun showTab(tab: String) {
        activeTab = tab
        updateNavSelection(tab)
        if (workspaceJson == null) {
            refreshWorkspace()
        } else {
            renderCurrentTab()
        }
    }

    private fun renderCurrentTab() {
        if (store.token.isBlank()) return
        binding.workspaceContent.removeAllViews()
        val data = workspaceJson
        if (data == null) {
            showLoading("Loading your workspace…")
            return
        }
        when (activeTab) {
            "table" -> renderTableOrdering(data)
            "availability" -> renderAvailability(data)
            else -> renderProfile(data)
        }
    }

    private fun renderProfile(data: JSONObject) {
        val profile = data.optJSONObject("profile") ?: JSONObject()
        val user = data.optJSONObject("user") ?: JSONObject()
        val root = screen("Profile", "Your staff account, attendance, and cafe work in one place.")
        val roles = jsonArrayText(user.optJSONArray("roles"))
        val identity = section("Welcome back", "Live account from Brownberries Cafe")
        identity.addView(text(user.optString("full_name"), 21, true))
        identity.addView(text(user.optString("email"), 14, false))
        identity.addView(badge("${roles.replace(", ", "  •  ")}"))
        root.addView(identity)

        val today = data.optJSONObject("attendance")?.optJSONObject("today")
        val attendance = section("Attendance", "Automatic geofence attendance runs in the background.")
        if (store.checkedIn) {
            attendance.addView(text("Checked in", 18, true))
            attendance.addView(text("Since ${store.activeCheckInAt.ifBlank { "today" }}  •  ${store.activeStatusLabel.ifBlank { "Present" }}"))
            attendance.addView(actionButton("Check-Out") { checkOut() })
        } else {
            attendance.addView(text(if (today == null) "Waiting for your automatic check-in." else prettyAttendance(today)))
            attendance.addView(actionButton("Refresh Attendance") { refreshBootstrap() })
        }
        root.addView(attendance)

        val shortcuts = section("Your workspace", "Quick access to the things staff use most.")
        val shortcutRow = horizontalScroll()
        addScrollItem(shortcutRow, smallAction("Attendance") { showProfileSubsection(data, "attendance") })
        addScrollItem(shortcutRow, smallAction("Leave") { showProfileSubsection(data, "leave") })
        addScrollItem(shortcutRow, smallAction("Documents") { showProfileSubsection(data, "documents") })
        addScrollItem(shortcutRow, smallAction("Rule Book") { showProfileSubsection(data, "rulebook") })
        shortcuts.addView(shortcutRow)
        root.addView(shortcuts)

        val personal = section("Personal Information", "Update your details; access-controlled records stay on the server.")
        val phone = input("Phone", profile.optString("phone"))
        val address = input("Address", profile.optString("address"))
        val gender = input("Gender", profile.optString("gender"))
        val marital = input("Marital status", profile.optString("marital_status"))
        val dob = input("DOB (YYYY-MM-DD)", profile.optString("dob"))
        val govtType = input("Government ID type", profile.optString("govt_id_type"))
        val govtNumber = input("Government ID number", profile.optString("govt_id_number"))
        listOf(phone, address, gender, marital, dob, govtType, govtNumber).forEach(personal::addView)
        personal.addView(actionButton("Save Profile") {
            val payload = JSONObject()
                .put("phone", phone.value()).put("address", address.value()).put("gender", gender.value())
                .put("marital_status", marital.value()).put("dob", dob.value())
                .put("govt_id_type", govtType.value()).put("govt_id_number", govtNumber.value())
            lifecycleScope.launch {
                runCatching { api.updateProfile(store.baseUrl, store.token, payload) }
                    .onSuccess { toast("Profile saved"); refreshWorkspace(silent = true) }
                    .onFailure { toast(it.message ?: "Profile save failed") }
            }
        })
        root.addView(personal)

        val rulebook = section("Attendance Rule Book", "Current rules published by cafe management.")
        val rulebookData = data.optJSONObject("rulebook")
        rulebook.addView(text(rulebookData?.optString("content") ?: "The current rule book is not available."))
        root.addView(rulebook)
        binding.workspaceContent.addView(root)
    }

    private fun showProfileSubsection(data: JSONObject, subsection: String) {
        binding.workspaceContent.removeAllViews()
        when (subsection) {
            "attendance" -> renderAttendance(data)
            "leave" -> renderLeave(data)
            "documents" -> renderDocuments(data)
            "rulebook" -> {
                val root = screen("Attendance Rule Book", "Read the latest workplace attendance policy.")
                val rulebook = data.optJSONObject("rulebook")
                root.addView(section(rulebook?.optString("title") ?: "Rules", "Version ${rulebook?.optInt("version", 1) ?: 1}").apply {
                    addView(text(rulebook?.optString("content") ?: "No rule book has been published."))
                })
                binding.workspaceContent.addView(root)
            }
        }
    }

    private fun renderAttendance(data: JSONObject) {
        val root = screen("Attendance", "Your check-in is automatic when you are inside the cafe perimeter.")
        val card = section(if (store.checkedIn) "Checked in now" else "No active check-in", "IST is used for all attendance records.")
        card.addView(text(if (store.checkedIn) "Since ${store.activeCheckInAt.ifBlank { "today" }}" else "The monitor will check again automatically."))
        if (store.checkedIn) card.addView(actionButton("Check-Out") { checkOut() })
        root.addView(card)
        root.addView(section("Today", "Latest server record").apply {
            addView(text(prettyAttendance(data.optJSONObject("attendance")?.optJSONObject("today"))))
        })
        val history = data.optJSONObject("attendance")?.optJSONArray("history") ?: JSONArray()
        val historyCard = section("Recent attendance", "Your latest attendance history")
        if (history.length() == 0) historyCard.addView(text("No attendance history yet."))
        for (i in 0 until history.length()) historyCard.addView(text(prettyAttendance(history.optJSONObject(i))))
        root.addView(historyCard)
        binding.workspaceContent.addView(root)
    }

    private fun renderLeave(data: JSONObject) {
        val root = screen("Leave", "Submit and track leave requests without leaving the app.")
        val leave = data.optJSONObject("leave") ?: JSONObject()
        val balance = leave.optJSONObject("balance") ?: JSONObject()
        root.addView(section("Leave balance", "Current server balance").apply {
            addView(text("Earned  ${balance.optDouble("earned", 0.0)} days   •   Urgent  ${balance.optDouble("urgent", 0.0)} days", 16, true))
        })
        val form = section("New leave request", "Use YYYY-MM-DD dates.")
        val type = Spinner(this).apply { adapter = simpleAdapter(listOf("earned", "urgent")); layoutParams = fieldParams() }
        val start = input("Start date (YYYY-MM-DD)")
        val end = input("End date (YYYY-MM-DD)")
        val reason = input("Reason")
        form.addView(label("Leave type")); form.addView(type); form.addView(start); form.addView(end); form.addView(reason)
        form.addView(actionButton("Submit Leave Request") {
            lifecycleScope.launch {
                runCatching { api.createLeave(store.baseUrl, store.token, JSONObject().put("leave_type", type.selectedItem.toString()).put("start_date", start.value()).put("end_date", end.value()).put("reason", reason.value())) }
                    .onSuccess { toast("Leave request submitted"); refreshWorkspace(silent = true) }
                    .onFailure { toast(it.message ?: "Leave request failed") }
            }
        })
        root.addView(form)
        val requests = leave.optJSONArray("requests") ?: JSONArray()
        root.addView(section("Requests", "Your submitted leave requests").apply {
            if (requests.length() == 0) addView(text("No leave requests yet."))
            for (i in 0 until requests.length()) {
                val row = requests.optJSONObject(i) ?: continue
                val line = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                line.addView(text("${row.optString("start_date")} → ${row.optString("end_date")}\n${row.optString("leave_type")}  •  ${row.optString("status")}", 14, false, 1f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                if (row.optString("status") == "pending") line.addView(smallAction("Cancel") {
                    lifecycleScope.launch { runCatching { api.cancelLeave(store.baseUrl, store.token, row.optInt("id")) }.onSuccess { refreshWorkspace(silent = true) }.onFailure { toast(it.message ?: "Cancel failed") } }
                })
                addView(line)
            }
        })
        binding.workspaceContent.addView(root)
    }

    private fun renderDocuments(data: JSONObject) {
        val root = screen("Documents", "Upload IDs and proofs for management review.")
        val form = section("Upload a document", "Your document stays in the cafe records and can be released to you by management.")
        val type = input("Document type, e.g. Aadhaar or PAN")
        val number = input("Identification number")
        form.addView(type); form.addView(number)
        form.addView(actionButton("Choose File") { documentPicker.launch("*/*") })
        form.addView(actionButton("Upload Document") {
            val uri = pendingDocumentUri
            if (uri == null) { toast("Choose a document first"); return@actionButton }
            lifecycleScope.launch {
                runCatching { api.uploadDocument(store.baseUrl, store.token, copyUriToCache(uri), type.value(), number.value()) }
                    .onSuccess { pendingDocumentUri = null; toast("Document uploaded for review"); refreshWorkspace(silent = true) }
                    .onFailure { toast(it.message ?: "Upload failed") }
            }
        })
        root.addView(form)
        val docs = data.optJSONArray("documents") ?: JSONArray()
        root.addView(section("Your documents", "Status and release information").apply {
            if (docs.length() == 0) addView(text("No documents uploaded yet."))
            for (i in 0 until docs.length()) {
                val doc = docs.optJSONObject(i) ?: continue
                addView(text("${doc.optString("doc_type")}  •  ${doc.optString("status")}\n${doc.optString("file_name")}", 15, true))
            }
        })
        binding.workspaceContent.addView(root)
    }

    private fun renderTableOrdering(data: JSONObject) {
        val root = screen("Table Ordering", "A fast native ordering workspace backed by the live cafe menu.")
        val tables = data.optJSONArray("tables") ?: JSONArray()
        val tableIds = mutableListOf<Int>()
        val tableButtons = mutableMapOf<Int, Button>()
        var selectedTableId: Int? = null
        var refreshLiveOrders: (() -> Unit)? = null
        val tableCard = section("Choose a table", "Select the table before adding items.")
        val tableRow = horizontalScroll()
        for (i in 0 until tables.length()) {
            val row = tables.optJSONObject(i) ?: continue
            val id = row.optInt("id")
            if (id <= 0) continue
            tableIds += id
            val button = smallAction("${row.optString("name")}\n${row.optInt("active_orders")} open") {
                selectedTableId = id
                tableButtons.values.forEach { it.background = rounded(Color.WHITE, 14) }
                buttonBackground(tableButtons[id], true)
                refreshLiveOrders?.invoke()
            }
            button.minWidth = dp(92); button.minHeight = dp(64)
            tableButtons[id] = button
            addScrollItem(tableRow, button)
        }
        selectedTableId = tableIds.firstOrNull()
        selectedTableId?.let { buttonBackground(tableButtons[it], true) }
        if (tableIds.isEmpty()) addScrollItem(tableRow, text("No active tables available."))
        tableCard.addView(tableRow); root.addView(tableCard)

        val liveOrdersCard = section("Live orders", "Current-day orders already running at the selected table.")
        val liveOrdersBody = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        liveOrdersCard.addView(liveOrdersBody)
        val tableById = (0 until tables.length()).mapNotNull { tables.optJSONObject(it) }.associateBy { it.optInt("id") }
        fun renderLiveOrders() {
            liveOrdersBody.removeAllViews()
            val table = tableById[selectedTableId]
            val orders = table?.optJSONArray("orders") ?: JSONArray()
            if (orders.length() == 0) {
                liveOrdersBody.addView(text("No open orders for this table today."))
                return
            }
            for (i in 0 until orders.length()) {
                val order = orders.optJSONObject(i) ?: continue
                val items = order.optJSONArray("items") ?: JSONArray()
                val itemText = (0 until items.length()).mapNotNull { j ->
                    items.optJSONObject(j)?.let { item -> "${item.optString("name")} × ${item.optInt("quantity", 1)}" }
                }.joinToString("  •  ")
                liveOrdersBody.addView(text("${order.optString("order_code")}  •  ${order.optString("status")}  •  ₹${"%.2f".format(order.optDouble("total", 0.0))}\n$itemText", 14, true))
            }
        }
        refreshLiveOrders = { renderLiveOrders() }
        renderLiveOrders()
        root.addView(liveOrdersCard)

        val menu = data.optJSONArray("menu") ?: JSONArray()
        val cart = mutableListOf<StaffCartLine>()
        val menuCard = section("Menu", "Search, filter, and add items with the same prices used by the cafe.")
        val search = input("Search food or drinks…")
        menuCard.addView(search)
        val categories = linkedSetOf("All")
        for (i in 0 until menu.length()) {
            val names = menu.optJSONObject(i)?.optJSONArray("category_names")
            for (j in 0 until (names?.length() ?: 0)) names?.optString(j)?.takeIf { it.isNotBlank() }?.let(categories::add)
        }
        val categoryRow = horizontalScroll(); menuCard.addView(categoryRow)
        val menuList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        menuCard.addView(menuList); root.addView(menuCard)

        val cartCard = section("Current order", "Review quantities before sending this table order for approval.")
        val cartBody = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        cartCard.addView(cartBody); root.addView(cartCard)
        var activeCategory = "All"
        lateinit var renderCart: () -> Unit

        renderCart = {
            cartBody.removeAllViews()
            if (cart.isEmpty()) {
                cartBody.addView(text("Cart is empty. Add a menu item above."))
            } else {
                var total = 0.0
                cart.forEachIndexed { index, line ->
                    val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                    row.addView(text("${line.item.optString("name")}  × ${line.quantity}\n${line.sizeLabel}${if (line.parcel) "  •  Parcel" else ""}", 14, true, 1f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                    row.addView(smallAction("−") { if (line.quantity > 1) line.quantity-- else cart.removeAt(index); renderCart() })
                    row.addView(smallAction("+") { line.quantity++; renderCart() })
                    row.addView(smallAction("Remove") { cart.removeAt(index); renderCart() })
                    cartBody.addView(row)
                    total += line.unitPrice * line.quantity + if (line.parcel) 20.0 * line.quantity else 0.0
                }
                cartBody.addView(text("Estimated total  ₹${"%.2f".format(total)}", 19, true))
                cartBody.addView(actionButton("Place Staff Order") {
                    val tableId = selectedTableId
                    if (tableId == null) { toast("Choose a table first"); return@actionButton }
                    val chosen = JSONArray()
                    cart.forEach { line ->
                        chosen.put(JSONObject().put("menu_item_id", line.item.optInt("id")).put("quantity", line.quantity).put("size_label", line.sizeLabel).put("is_parcel", line.parcel))
                    }
                    lifecycleScope.launch {
                        runCatching { api.createOrder(store.baseUrl, store.token, JSONObject().put("table_id", tableId).put("items", chosen)) }
                            .onSuccess { cart.clear(); renderCart(); toast("Order sent for approval"); refreshWorkspace(silent = true) }
                            .onFailure { toast(it.message ?: "Order failed") }
                    }
                })
            }
        }

        fun buildMenu() {
            val query = search.value().lowercase()
            menuList.removeAllViews()
            var shown = 0
            for (i in 0 until menu.length()) {
                val item = menu.optJSONObject(i) ?: continue
                val categoriesForItem = jsonArrayValues(item.optJSONArray("category_names"))
                val searchable = listOf(item.optString("name"), item.optString("short_description"), categoriesForItem.joinToString(" ")).joinToString(" ").lowercase()
                if (activeCategory != "All" && categoriesForItem.none { it.equals(activeCategory, true) }) continue
                if (query.isNotBlank() && fuzzyScore(searchable, query) < 0.45) continue
                shown++
                val tile = tileCard()
                tile.addView(text(item.optString("name"), 18, true))
                tile.addView(text(item.optString("short_description").ifBlank { categoriesForItem.joinToString("  •  ") }, 13, false))
                addMenuImage(tile, item.optString("image_url"))
                val priceText = text("₹${"%.2f".format(item.optDouble("price", 0.0))}", 17, true)
                tile.addView(priceText)
                var selectedSize = "Standard"
                var selectedPrice = item.optDouble("price", 0.0)
                val options = item.optJSONArray("sizes")
                if (options != null && options.length() > 0) {
                    tile.addView(label("Serving Size/Options"))
                    val labels = (0 until options.length()).map { j ->
                        val option = options.optJSONObject(j) ?: JSONObject()
                        "${option.optString("size")}  |  ₹${"%.2f".format(option.optDouble("price", 0.0))}"
                    }
                    val size = Spinner(this).apply { adapter = simpleAdapter(labels); layoutParams = fieldParams() }
                    tile.addView(size)
                    fun syncSize() {
                        val option = options.optJSONObject(size.selectedItemPosition) ?: JSONObject()
                        selectedSize = option.optString("size").ifBlank { "Standard" }
                        selectedPrice = option.optDouble("price", selectedPrice)
                        priceText.text = "₹${"%.2f".format(selectedPrice)}"
                    }
                    size.setOnItemSelectedListener(object : android.widget.AdapterView.OnItemSelectedListener {
                        override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
                        override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) = syncSize()
                    })
                    syncSize()
                }
                val parcel = CheckBox(this).apply { text = "Parcel  (+₹20 each)"; textSize = 13f }
                tile.addView(parcel)
                var quantity = 1
                val quantityText = text("1", 16, true, 0f).apply { gravity = Gravity.CENTER; minWidth = dp(34) }
                val controls = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                controls.addView(smallAction("−") { quantity = (quantity - 1).coerceAtLeast(1); quantityText.text = quantity.toString() })
                controls.addView(quantityText)
                controls.addView(smallAction("+") { quantity++; quantityText.text = quantity.toString() })
                controls.addView(actionButton("Add") {
                    val existing = cart.firstOrNull { it.item.optInt("id") == item.optInt("id") && it.sizeLabel == selectedSize && it.parcel == parcel.isChecked }
                    if (existing != null) existing.quantity += quantity else cart += StaffCartLine(item, quantity, selectedSize, selectedPrice, parcel.isChecked)
                    renderCart(); toast("${item.optString("name")} × $quantity added")
                })
                tile.addView(controls)
                menuList.addView(tile)
            }
            if (shown == 0) menuList.addView(text("No matching items found.", 15, true))
        }
        categories.forEach { category -> addScrollItem(categoryRow, smallAction(category) { activeCategory = category; buildMenu() }) }
        search.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = buildMenu()
            override fun afterTextChanged(s: Editable?) = Unit
        })
        buildMenu()
        renderCart()
    }

    private fun renderAvailability(data: JSONObject) {
        val root = screen("Items Availability", "Make live menu items available or unavailable for guests and staff.")
        val search = input("Search menu items…")
        root.addView(search)
        val status = text("Changes save automatically.", 13, false)
        root.addView(status)
        val list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(list)
        val availability = data.optJSONArray("availability_menu") ?: JSONArray()
        fun buildList() {
            list.removeAllViews()
            val query = search.value()
            var shown = 0
            for (i in 0 until availability.length()) {
                val item = availability.optJSONObject(i) ?: continue
                if (query.isNotBlank() && !item.optString("name").contains(query, true)) continue
                shown++
                val card = tileCard()
                val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
                row.addView(text(item.optString("name"), 16, true, 1f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                val check = CheckBox(this).apply { isChecked = item.optBoolean("available", true); contentDescription = "Availability for ${item.optString("name")}" }
                check.setOnCheckedChangeListener { _, checked ->
                    status.text = "Saving ${item.optString("name")}…"
                    lifecycleScope.launch {
                        runCatching { api.updateAvailability(store.baseUrl, store.token, item.optInt("id"), checked) }
                            .onSuccess { status.text = "Saved — live for guests and staff." }
                            .onFailure { status.text = it.message ?: "Availability update failed"; check.setOnCheckedChangeListener(null); check.isChecked = !checked }
                    }
                }
                row.addView(check); card.addView(row); list.addView(card)
            }
            if (shown == 0) list.addView(text("No menu items match this search."))
        }
        search.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = buildList()
            override fun afterTextChanged(s: Editable?) = Unit
        })
        buildList()
        binding.workspaceContent.addView(root)
    }

    private fun addMenuImage(parent: LinearLayout, rawUrl: String) {
        if (rawUrl.isBlank()) return
        val image = ImageView(this).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(170)).apply { topMargin = dp(8) }
            scaleType = ImageView.ScaleType.CENTER_CROP
            background = rounded(Color.rgb(248, 241, 234), 14)
            contentDescription = "Menu item image"
        }
        parent.addView(image)
        val url = if (rawUrl.startsWith("http")) rawUrl else "${store.baseUrl.trimEnd('/')}/${rawUrl.trimStart('/')}"
        lifecycleScope.launch(Dispatchers.IO) {
            runCatching { URL(url).openStream().use(BitmapFactory::decodeStream) }.getOrNull()?.let { bitmap ->
                withContext(Dispatchers.Main) { image.setImageBitmap(bitmap) }
            }
        }
    }

    private fun screen(title: String, subtitle: String): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(2), dp(10), dp(2), dp(28))
        addView(text(title, 28, true))
        addView(text(subtitle, 14, false))
    }

    private fun section(title: String, subtitle: String? = null): LinearLayout = tileCard().apply {
        addView(text(title, 19, true))
        if (!subtitle.isNullOrBlank()) addView(text(subtitle, 13, false))
    }

    private fun tileCard(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(14), dp(16), dp(14))
        background = rounded(Color.WHITE, 18)
        elevation = dp(2).toFloat()
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(10) }
    }

    private fun horizontalScroll(): HorizontalScrollView = HorizontalScrollView(this).apply {
        isHorizontalScrollBarEnabled = false
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(6) }
        addView(LinearLayout(this@MainActivity).apply { orientation = LinearLayout.HORIZONTAL })
    }

    private fun addScrollItem(parent: HorizontalScrollView, child: View) {
        (parent.getChildAt(0) as LinearLayout).addView(child)
    }

    private fun input(hint: String, value: String = ""): EditText = EditText(this).apply {
        this.hint = hint; setText(value); setTextColor(ink); setHintTextColor(mutedInk)
        setPadding(dp(12), dp(8), dp(12), dp(8)); minHeight = dp(48); background = rounded(Color.WHITE, 10)
        layoutParams = fieldParams()
    }

    private fun fieldParams() = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(8) }
    private fun label(value: String): TextView = text(value, 14, true)

    private fun text(value: String, size: Int = 14, bold: Boolean = false, weight: Float? = null): TextView = TextView(this).apply {
        text = value; textSize = size.toFloat(); setTextColor(ink); if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
        layoutParams = LinearLayout.LayoutParams(if (weight != null) 0 else LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(6) }
    }

    private fun badge(value: String): TextView = text(value, 12, true).apply {
        setTextColor(teal); background = rounded(Color.rgb(226, 243, 239), 10); setPadding(dp(10), dp(6), dp(10), dp(6))
    }

    private fun actionButton(label: String, action: () -> Unit): Button = Button(this).apply {
        text = label; isAllCaps = false; setOnClickListener { action() }; setTextColor(Color.WHITE)
        background = rounded(cocoa, 12); minHeight = dp(44); setPadding(dp(14), 0, dp(14), 0)
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(8); marginEnd = dp(6) }
    }

    private fun smallAction(label: String, action: () -> Unit): Button = actionButton(label, action).apply {
        minHeight = dp(42); textSize = 13f; background = rounded(Color.rgb(246, 236, 226), 12); setTextColor(ink)
    }

    private fun buttonBackground(button: Button?, selected: Boolean) {
        button ?: return
        button.background = rounded(if (selected) Color.rgb(242, 222, 199) else Color.WHITE, 14)
        button.setTextColor(ink)
    }

    private fun updateNavSelection(tab: String) {
        val inactive = Color.rgb(255, 250, 246)
        listOf("profile" to binding.profileTabButton, "table" to binding.tableOrderingTabButton, "availability" to binding.availabilityTabButton).forEach { (key, button) ->
            button.background = rounded(if (key == tab) cocoa else inactive, 12)
            button.setTextColor(if (key == tab) Color.WHITE else ink)
        }
        binding.logoutNavButton.background = rounded(darkCocoa, 12)
        binding.logoutNavButton.setTextColor(Color.WHITE)
    }

    private fun renderState() {
        val loggedIn = store.token.isNotBlank() && store.userName.isNotBlank()
        binding.loginBrand.visibility = if (loggedIn) View.GONE else View.VISIBLE
        binding.loginCard.visibility = if (loggedIn) View.GONE else View.VISIBLE
        binding.rootScroll.visibility = if (loggedIn) View.GONE else View.VISIBLE
        binding.workspaceNav.visibility = if (loggedIn) View.VISIBLE else View.GONE
        binding.workspaceScroll.visibility = if (loggedIn) View.VISIBLE else View.GONE
        if (loggedIn) updateNavSelection(activeTab) else binding.workspaceContent.removeAllViews()
    }

    private fun showLoading(message: String) {
        binding.workspaceContent.removeAllViews()
        binding.workspaceContent.addView(section("Loading live workspace", message).apply { addView(text("Connecting to ${store.baseUrl}", 13, false)) })
    }

    private fun showError(message: String) {
        binding.workspaceContent.removeAllViews()
        binding.workspaceContent.addView(section("Workspace unavailable", "The app could not read the live staff workspace.").apply {
            addView(text(message, 14, false)); addView(actionButton("Try Again") { refreshWorkspace() })
        })
    }

    private fun rounded(fill: Int, radius: Int): GradientDrawable = GradientDrawable().apply { setColor(fill); cornerRadius = dp(radius).toFloat() }
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
    private fun simpleAdapter(values: List<String>): ArrayAdapter<String> = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, values)
    private fun jsonArrayValues(array: JSONArray?): List<String> = if (array == null) emptyList() else (0 until array.length()).map { array.optString(it) }.filter { it.isNotBlank() }
    private fun jsonArrayText(array: JSONArray?): String = jsonArrayValues(array).ifEmpty { listOf("Staff") }.joinToString(", ")
    private fun prettyAttendance(row: JSONObject?): String = if (row == null) "No attendance recorded today." else "${row.optString("date")}  •  ${row.optString("status")}\nIn  ${row.optString("check_in_at", "—")}\nOut  ${row.optString("check_out_at", "—")}"
    private fun fuzzyScore(haystack: String, needle: String): Double {
        if (haystack.contains(needle, true)) return 1.0
        val words = needle.split(" ").filter { it.isNotBlank() }
        if (words.isEmpty()) return 1.0
        return words.count { word -> haystack.contains(word, true) }.toDouble() / words.size
    }
    private fun copyUriToCache(uri: Uri): File {
        val file = File(cacheDir, "staff-document-${System.currentTimeMillis()}")
        contentResolver.openInputStream(uri).use { input -> file.outputStream().use { output -> input?.copyTo(output) } }
        return file
    }
    private fun nowIso(): String = ZonedDateTime.now(ZoneId.of("Asia/Kolkata")).toString()
    private fun toast(message: String) = Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
}

private fun EditText.value(): String = text?.toString()?.trim().orEmpty()
