// Recompute and update time_bucket_v2 for recent documents so tags age correctly
// Usage (mongosh):
//   mongosh "$MONGO_URI/warrantdb" scripts/rebucket_time_bucket_v2.js
// Env overrides: DB_NAME, COLL, MAX_DAYS (default 90), DRY_RUN (set to '1' for no writes)

const DB_NAME = (typeof process !== 'undefined' && process.env.DB_NAME) || 'warrantdb';
const COLL = (typeof process !== 'undefined' && process.env.COLL) || 'simple_harris';
const MAX_DAYS = parseInt(((typeof process !== 'undefined' && process.env.MAX_DAYS) || '90'), 10);
const DRY_RUN = (typeof process !== 'undefined' && process.env.DRY_RUN) === '1';

const dbh = db.getSiblingDB(DB_NAME);
const coll = dbh.getCollection(COLL);

print(`\n[rebucket_time_bucket_v2] Database=${DB_NAME} Collection=${COLL} MAX_DAYS=${MAX_DAYS} DRY_RUN=${DRY_RUN}`);
print(`[rebucket_time_bucket_v2] Now: ${new Date().toISOString()}\n`);

const hoursSinceExpr = { $dateDiff: { startDate: "$booking_datetime", endDate: "$$NOW", unit: "hour" } };

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

// Update using an aggregation pipeline to compute the new bucket on the fly
const filter = {
  booking_datetime: { $type: "date" },
  $expr: { $lte: [ { $dateDiff: { startDate: "$booking_datetime", endDate: "$$NOW", unit: "day" } }, MAX_DAYS ] }
};

const pipeline = [
  { $set: { time_bucket_v2: computedBucketExpr } }
];

// Preview counts
const preview = coll.aggregate([
  { $match: filter },
  { $addFields: { computed: computedBucketExpr } },
  { $group: { _id: "$computed", n: { $sum: 1 } } },
  { $sort: { n: -1 } }
]).toArray();

print("[preview] New bucket distribution if applied:");
printjson(preview);

if (DRY_RUN) {
  print("[rebucket_time_bucket_v2] DRY_RUN=1, not applying updates.\n");
} else {
  const res = coll.updateMany(filter, pipeline);
  print("[rebucket_time_bucket_v2] updateMany result:");
  printjson(res);
}

print("\n[rebucket_time_bucket_v2] Done.\n");
