// Normalize booking_datetime to Date type and recompute time_bucket_v2
// - Converts booking_datetime strings (ISO or YYYY-MM-DD) to Date (UTC)
// - If booking_datetime missing, derives from booking_date (string/date)
// - Recomputes time_bucket_v2 based on hours since booking_datetime
// Usage (mongosh):
//   DRY_RUN=1 mongosh "$MONGO_URI/warrantdb" scripts/backfill_booking_datetime_from_strings.js
//   mongosh "$MONGO_URI/warrantdb" scripts/backfill_booking_datetime_from_strings.js

const DB_NAME = (typeof process !== 'undefined' && process.env.DB_NAME) || 'warrantdb';
const COLL = (typeof process !== 'undefined' && process.env.COLL) || 'simple_harris';
const DRY_RUN = (typeof process !== 'undefined' && process.env.DRY_RUN) === '1';

const dbh = db.getSiblingDB(DB_NAME);
const coll = dbh.getCollection(COLL);

print(`\n[backfill_booking_datetime_from_strings] Database=${DB_NAME} Collection=${COLL} DRY_RUN=${DRY_RUN}`);
print(`[backfill_booking_datetime_from_strings] Now: ${new Date().toISOString()}\n`);

const hoursSinceExprFrom = (dateExpr) => ({ $dateDiff: { startDate: dateExpr, endDate: "$$NOW", unit: "hour" } });

const computedBucketFrom = (dateExpr) => ({
  $let: {
    vars: { hrs: hoursSinceExprFrom(dateExpr) },
    in: {
      $switch: {
        branches: [
          { case: { $and: [ { $gte: ["$$hrs", 0] },   { $lt: ["$$hrs", 24] } ] }, then: "0_24h" },
          { case: { $and: [ { $gte: ["$$hrs", 24] },  { $lt: ["$$hrs", 48] } ] }, then: "24_48h" },
          { case: { $and: [ { $gte: ["$$hrs", 48] },  { $lt: ["$$hrs", 72] } ] }, then: "48_72h" },
          { case: { $and: [ { $gte: ["$$hrs", 72] },  { $lt: ["$$hrs", 168] } ] }, then: "3d_7d" },
          { case: { $and: [ { $gte: ["$$hrs", 168] }, { $lt: ["$$hrs", 720] } ] }, then: "7d_30d" },
          { case: { $and: [ { $gte: ["$$hrs", 720] }, { $lt: ["$$hrs", 1440] } ] }, then: "30d_60d" },
          { case: { $gte: ["$$hrs", 1440] }, then: "60d_plus" }
        ],
        default: null
      }
    }
  }
});

// 0) Pre-stats
const pre = coll.aggregate([
  { $group: { _id: { dtType: { $type: "$booking_datetime" }, bucketType: { $type: "$time_bucket_v2" } }, n: { $sum: 1 } } },
  { $sort: { n: -1 } }
]).toArray();
print("[pre] booking_datetime/time_bucket_v2 type distribution:");
printjson(pre);

// 1) Convert booking_datetime strings to Date
const filter1 = {
  booking_datetime: { $type: "string", $regex: /^\d{4}-\d{2}-\d{2}/ }
};
const pipeline1 = [
  { $set: {
      booking_datetime: { $dateFromString: { dateString: "$booking_datetime", timezone: "UTC" } },
      booking_date_v2: { $substrBytes: ["$booking_datetime", 0, 10] },
      booking_derivation_source: "booking_datetime_string",
      time_bucket_v2: computedBucketFrom({ $dateFromString: { dateString: "$booking_datetime", timezone: "UTC" } })
  } }
];

// 2) Derive booking_datetime from booking_date string when missing
const filter2 = {
  booking_datetime: { $exists: false },
  booking_date: { $type: "string", $regex: /^\d{4}-\d{2}-\d{2}/ }
};
const pipeline2 = [
  { $set: {
      booking_datetime: { $dateFromString: { dateString: "$booking_date", timezone: "UTC" } },
      booking_date_v2: "$booking_date",
      booking_derivation_source: "legacy_booking_date",
      time_bucket_v2: computedBucketFrom({ $dateFromString: { dateString: "$booking_date", timezone: "UTC" } })
  } }
];

// 3) Derive booking_datetime from booking_date date type when missing
const filter3 = {
  booking_datetime: { $exists: false },
  booking_date: { $type: "date" }
};
const pipeline3 = [
  { $set: {
      booking_datetime: "$booking_date",
      booking_date_v2: { $dateToString: { date: "$booking_date", format: "%Y-%m-%d", timezone: "UTC" } },
      booking_derivation_source: "legacy_booking_date",
      time_bucket_v2: computedBucketFrom("$booking_date")
  } }
];

if (DRY_RUN) {
  const n1 = coll.countDocuments(filter1);
  const n2 = coll.countDocuments(filter2);
  const n3 = coll.countDocuments(filter3);
  print(`[dry-run] Will convert booking_datetime strings -> Date: ${n1}`);
  print(`[dry-run] Will derive from booking_date (string): ${n2}`);
  print(`[dry-run] Will derive from booking_date (date): ${n3}`);
} else {
  const r1 = coll.updateMany(filter1, pipeline1);
  print("[update] booking_datetime string -> Date:");
  printjson(r1);

  const r2 = coll.updateMany(filter2, pipeline2);
  print("[update] derive from booking_date (string):");
  printjson(r2);

  const r3 = coll.updateMany(filter3, pipeline3);
  print("[update] derive from booking_date (date):");
  printjson(r3);
}

// 4) Post-stats
const post = coll.aggregate([
  { $group: { _id: { dtType: { $type: "$booking_datetime" }, bucketType: { $type: "$time_bucket_v2" } }, n: { $sum: 1 } } },
  { $sort: { n: -1 } }
]).toArray();
print("[post] booking_datetime/time_bucket_v2 type distribution:");
printjson(post);

print("\n[backfill_booking_datetime_from_strings] Done.\n");
