// scripts/field_inventory.js
// Inventory top-level fields and common nested array-object fields for each source collection.
// Writes to STDOUT so you can redirect into a file.
//
// Usage:
//   mongosh "$MONGO_URI" --quiet --file scripts/field_inventory.js > debug/field_inventory_YYYYMMDDTHHMMSSZ.txt
//
// Notes:
// - Limits are applied to keep scans fast and avoid timeouts.
// - No fancy operators (e.g., $regexReplace) so this works on Atlas 6.x/7.x/8.x.

(function () {
  // Ensure the script lands on the right DB even when using a full SRV URI
  db = db.getSiblingDB("warrantdb");
  print("# Connected to DB:", db.getName());

  const LIMIT_TOP = 2000;   // docs to scan for top-level fields
  const LIMIT_ARR = 5000;   // array elements to sample
  const SIMPLE_SAMPLE_FILTER = {}; // customize if you want to scope by date, etc.

  function listTopLevelFields(collName, sampleFilter = SIMPLE_SAMPLE_FILTER) {
    const out = db.getCollection(collName).aggregate([
      { $match: sampleFilter },
      { $limit: LIMIT_TOP },
      { $project: { kv: { $objectToArray: "$$ROOT" } } },
      { $unwind: "$kv" },
      { $group: { _id: null, fields: { $addToSet: "$kv.k" } } }
    ], { allowDiskUse: true }).toArray();

    const fields = (out[0]?.fields || []).sort();
    print(`\n# ${collName} — top-level fields (${fields.length})`);
    fields.forEach(f => print("-", f));
  }

  function sampleDoc(collName, sampleFilter = SIMPLE_SAMPLE_FILTER) {
    print(`\n# ${collName} — one sample doc`);
    const doc = db.getCollection(collName).findOne(sampleFilter);
    if (doc) { printjson(doc); }
    else { print("(no document found)"); }
  }

  function listArrayObjectFields(collName, arrayPath, sampleFilter = SIMPLE_SAMPLE_FILTER) {
    const out = db.getCollection(collName).aggregate([
      { $match: sampleFilter },
      { $limit: LIMIT_TOP },
      { $project: { arr: `$${arrayPath}` } },
      { $unwind: { path: "$arr", preserveNullAndEmptyArrays: false } },
      { $limit: LIMIT_ARR },
      { $project: { kv: { $objectToArray: "$arr" } } },
      { $unwind: "$kv" },
      { $group: { _id: null, fields: { $addToSet: "$kv.k" } } }
    ], { allowDiskUse: true }).toArray();

    const fields = (out[0]?.fields || []).sort();
    print(`\n# ${collName}.${arrayPath} — fields (${fields.length})`);
    fields.forEach(f => print("-", f));
  }

  // ---- Collections to inspect ----
  const collections = [
    "brazoria_inmates",
    "fortbend_inmates",
    "galveston_events",
    "jefferson_events",
    "harris_bond",
    "harris_misfel",
    "harris_nafiling",
  ];

  // ---- Run inventories ----
  collections.forEach(c => {
    listTopLevelFields(c);
    sampleDoc(c);
  });

  // Arrays we commonly expect to be an array of objects:
  const arraySpecs = [
    ["brazoria_inmates", "charges"],
    ["fortbend_inmates", "charges"],
    ["galveston_events", "charges"],
    ["galveston_events", "events"],
    ["jefferson_events", "charges"],
    ["jefferson_events", "events"],
  ];

  arraySpecs.forEach(([c, a]) => {
    try { listArrayObjectFields(c, a); } catch (e) { /* skip if path missing */ }
  });

  // Document counts for quick sanity check
  print("\n# Document counts");
  collections.forEach(c => {
    const n = db.getCollection(c).countDocuments({});
    print(c, n);
  });

  print("\n# Done.");
})();