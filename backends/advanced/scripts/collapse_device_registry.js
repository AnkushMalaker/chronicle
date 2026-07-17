// One-time migration: collapse the bloated `registered_clients` map.
//
// Before stable client_ids existed, every reconnect of one device minted a new
// counter-suffixed client_id (havpe, havpe-2, … havpe-286), so the registry
// accumulated hundreds of rows for a handful of physical devices. This script
// rewrites each user's `registered_clients` keyed by the STABLE client_id
// (user_suffix + sanitized device_name) — one row per device — merging the
// counter duplicates and preserving any user-set friendly name + the earliest
// first_seen / latest last_seen.
//
// IDEMPOTENT: re-running yields the same result. Run it AFTER the stable-id
// backend code is deployed (otherwise the old counter immediately re-pollutes).
//
//   docker exec <mongo> mongosh chronicle --file /path/collapse_device_registry.js
//   DRY_RUN: set env DRYRUN=1 (default) to preview; DRYRUN=0 to apply.

// mongosh exposes process.env
const APPLY = (typeof process !== "undefined" && process.env && process.env.DRYRUN === "0");

// Mirror backend generate_client_id sanitization:
// lowercase, keep [a-z0-9-], first 10 chars.
function sanitizeDevice(d) {
  return String(d || "")
    .toLowerCase()
    .split("")
    .filter((c) => /[a-z0-9-]/.test(c))
    .join("")
    .slice(0, 10);
}

function stableClientId(userIdHex, deviceName) {
  return userIdHex.slice(-6) + "-" + sanitizeDevice(deviceName);
}

let usersTouched = 0;
let totalBefore = 0;
let totalAfter = 0;

db.users.find({ "registered_clients": { $exists: true, $ne: {} } }).forEach((u) => {
  const idHex = u._id.toString();
  const rc = u.registered_clients || {};
  const keys = Object.keys(rc);
  if (keys.length === 0) return;

  const collapsed = {};
  keys.forEach((k) => {
    const e = rc[k] || {};
    // Without a device_name we can't derive a stable id — keep the row as-is.
    const stableId = e.device_name ? stableClientId(idHex, e.device_name) : k;

    const cur = collapsed[stableId];
    if (!cur) {
      collapsed[stableId] = {
        client_id: stableId,
        device_name: e.device_name || null,
        name: e.name || e.device_name || stableId,
        first_seen: e.first_seen || e.last_seen || new Date(),
        last_seen: e.last_seen || e.first_seen || new Date(),
      };
    } else {
      // Merge duplicates: keep a real user-set name, widen the time range.
      if ((!cur.name || cur.name === cur.device_name) && e.name && e.name !== e.device_name) {
        cur.name = e.name;
      }
      if (e.first_seen && e.first_seen < cur.first_seen) cur.first_seen = e.first_seen;
      if (e.last_seen && e.last_seen > cur.last_seen) cur.last_seen = e.last_seen;
    }
  });

  const before = keys.length;
  const after = Object.keys(collapsed).length;
  totalBefore += before;
  totalAfter += after;
  if (after === before) return; // nothing to collapse for this user

  usersTouched += 1;
  print(`${u.email}: ${before} -> ${after}  [${Object.keys(collapsed).join(", ")}]`);

  if (APPLY) {
    db.users.updateOne({ _id: u._id }, { $set: { registered_clients: collapsed } });
  }
});

print("");
print(`${APPLY ? "APPLIED" : "DRY RUN (set DRYRUN=0 to apply)"}: ` +
      `users touched=${usersTouched}, entries ${totalBefore} -> ${totalAfter}`);
