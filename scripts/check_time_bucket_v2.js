// Verify time_bucket_v2 correctness vs a live computed bucket and inspect 24–48h cohort
// Usage (mongosh):
//   mongosh "$MONGO_URI/warrantdb" scripts/check_time_bucket_v2.js
// Optionally override DB/COL via env or edit below.

const DB_NAME = (typeof process !== 'undefined' && process.env.DB_NAME) || 'warrantdb';
const COLL = (typeof process !== 'undefined' && process.env.COLL) || 'simple_harris';

const dbh = db.getSiblingDB(DB_NAME);
const coll = dbh.getCollection(COLL);

print(`\n[check_time_bucket_v2] Database=${DB_NAME} Collection=${COLL}`);
print(`[check_time_bucket_v2] Now: ${new Date().toISOString()}\n`);

// Reusable expressions
const hoursSinceExpr = {
  $dateDiff: { startDate: "$booking_datetime", endDate: "$$NOW", unit: "hour" }
};

const computedBucketExpr = {
  $let: {
    vars: { hrs: hoursSinceExpr },
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
};

// 1) Count documents with booking_datetime in [24, 48) hours
const count24to48 = coll.aggregate([
  { $match: { booking_datetime: { $type: "date" } } },
  { $addFields: { hrs: hoursSinceExpr } },
  { $match: { hrs: { $gte: 24, $lt: 48 } } },
  { $count: "n" }
]).toArray();

const n24to48 = (count24to48[0] && count24to48[0].n) || 0;
print(`[1] Live computed count in 24–48h window: ${n24to48}`);

// 2) Among those, how are stored tags distributed?
const dist24to48 = coll.aggregate([
  { $match: { booking_datetime: { $type: "date" } } },
  { $addFields: { hrs: hoursSinceExpr } },
  { $match: { hrs: { $gte: 24, $lt: 48 } } },
  { $group: { _id: "$time_bucket_v2", n: { $sum: 1 } } },
  { $sort: { n: -1 } }
]).toArray();

print("[2] Stored time_bucket_v2 distribution inside 24–48h cohort:");
printjson(dist24to48);

// 3) Cross-tab of stored vs live-computed buckets for recent docs (last 90 days)
const crossTab = coll.aggregate([
  { $match: { booking_datetime: { $type: "date" } } },
  { $addFields: { hrs: hoursSinceExpr } },
  { $match: { hrs: { $gte: 0, $lt: 2160 } } }, // up to ~90 days
  { $project: { stored: "$time_bucket_v2", computed: computedBucketExpr } },
  { $group: { _id: { stored: "$stored", computed: "$computed" }, n: { $sum: 1 } } },
  { $sort: { n: -1 } }
]).toArray();

print("[3] Stored vs computed cross-tab (last 90 days):");
printjson(crossTab);

// 4) Sample mismatches
const mismatches = coll.aggregate([
  { $match: { booking_datetime: { $type: "date" } } },
  { $addFields: { hrs: hoursSinceExpr, computed: computedBucketExpr } },
  { $match: { $expr: { $ne: ["$time_bucket_v2", "$computed"] } } },
  { $project: { _id: 1, booking_datetime: 1, hrs: 1, stored: "$time_bucket_v2", computed: 1 } },
  { $sort: { hrs: 1 } },
  { $limit: 20 }
]).toArray();

print("[4] Sample mismatches (up to 20):");
printjson(mismatches);

print("\n[check_time_bucket_v2] Done.\n");
