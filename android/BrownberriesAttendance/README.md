# Brownberries Staff

Android-first staff workspace for Brownberries Cafe.

## What this app does

- Staff logs in with their existing Brownberries staff account
- Staff can manage their profile, attendance history, leave requests, and documents
- Staff can place table orders and update menu-item availability from the Cafe tab
- Before sign-in, the home screen shows only a clean email/password form
- After sign-in, the top navigation is Profile, Table Ordering, Item Availability, and Sign Out
- App downloads the cafe geofence and the assigned staff shift window
- A foreground location monitor checks position every minute
- When the staff member is inside the geofence, the app can auto check-in
- While checked in, the app sends heartbeats to the server
- If location repeatedly fails while internet is available, the app auto checks out after the grace period
- If internet is unavailable, the app waits longer, queues the checkout locally, and syncs it when the network returns
- Monitoring starts automatically after login, at every app launch, after device restart, and after an app update
- The only manual attendance action is **Check-Out**; automatic checkout remains controlled by the server policy and monitor
- The staff workspaces are native Android screens backed by the existing production APIs. The app has its own four-tab navigation and does not embed the web UI.

## Current backend endpoints used

- `POST /api/mobile/attendance/login`
- `GET /api/mobile/attendance/bootstrap`
- `POST /api/mobile/attendance/check-in`
- `POST /api/mobile/attendance/heartbeat`
- `POST /api/mobile/attendance/check-out`
- `POST /api/mobile/attendance/logout`

### Staff workspace endpoints

- `GET /api/mobile/staff/workspace`
- `POST /api/mobile/staff/profile`
- `POST /api/mobile/staff/leaves`
- `POST /api/mobile/staff/leaves/<id>/cancel`
- `POST /api/mobile/staff/documents`
- `GET /api/mobile/staff/documents/<id>/download`
- `POST /api/mobile/staff/items/<id>/availability`
- `POST /api/mobile/staff/orders`

The native app uses the bearer token returned by the attendance login for every
workspace request. The Windows server must be running the matching source
revision containing `app/mobile_staff.py`; after deployment,
`GET /api/mobile/staff/workspace` should return `401` when called without a
token (not `404`).

## Build in Android Studio

1. Open Android Studio
2. Choose `Open`
3. Select this folder:
   - `.../Brownberries Cafe Operations/android/BrownberriesAttendance`
4. Let Gradle sync
5. Run on an Android phone with Google Play Services enabled

## Server URL

Use either:

- Production domain: `https://brownberriescafe.com`
- Local network server during testing: for example `http://192.168.x.x:5050`

## Permissions to allow on the phone

- Fine location
- Background location
- Notifications

On Samsung devices, set the app battery mode to **Unrestricted** and allow
background activity. Also allow **Location > Allow all the time** and enable
notifications. Android may stop a location service after a user uses **Force
stop**; no app can restart itself after that system action. Closing the app
normally leaves the foreground monitor running. The monitor is a foreground
service, restarts after normal process termination, and is started again after
device boot or app replacement when the staff session is still active.

## Important notes

- This version is Android-first on purpose
- It does not collect screen-time data
- iPhone support should be built later with a separate platform-specific behavior model
- If Android background-location policy changes, the app may need a Play-policy review before public distribution
