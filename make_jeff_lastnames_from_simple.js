// make_jeff_lastnames_from_simple.js
// Build a de-duplicated, frequency-ranked surname list from normalized collections,
// printing ONE name per line (ideal for redirecting to a text file).

// 1) Select the correct database explicitly (Atlas default is "test")
db = db.getSiblingDB("warrantdb");

// 2) Normalized collections to mine
const SIMPLE_COLL_NAMES = [
  "simple_brazoria",
  "simple_fortbend",
  "simple_galveston",
  "simple_harris",
  "simple_jefferson",
];

// 3) Helpers to extract and normalize last names

function cleanLast(raw) {
  if (!raw) return null;
  let last = String(raw).trim().toUpperCase();

  // Allow letters, spaces, hyphens, and apostrophes; collapse multiples
  last = last.replace(/\s+/g, " ").replace(/[^A-Z\- ']/g, "");

  // Drop a terminal suffix if present (JR/SR/II/III/IV/V)
  last = last.replace(/\b(JR|SR|II|III|IV|V)\b\s*$/,"").trim();

  // Guardrails
  if (last.length < 2) return null;

  // Collapse any double spaces again
  last = last.replace(/\s{2,}/g, " ");
  return last;
}

function extractLastFromDoc(doc) {
  // Prefer normalized last name fields
  const last =
    (doc.last) ||
    (doc.last_name) ||
    (doc.surname) ||
    (doc.person && (doc.person.last || doc.person.last_name || doc.person.surname)) ||
    null;

  if (typeof last === "string" && last.trim()) {
    return cleanLast(last);
  }

  // Fallback to a name string
  const name =
    (doc.name) ||
    (doc.full_name) ||
    (doc.person && (doc.person.name || doc.person.full_name)) ||
    null;

  if (!name || typeof name !== "string") return null;

  // If in "LAST, FIRST" format, prefer the part before the comma
  const rawLast = name.includes(",")
    ? name.split(",", 1)[0]
    : (name.trim().split(/\s+/).pop());

  if (!rawLast) return null;
  const bad = new Set(["UNKNOWN", "UNK", "N/A", "NA"]);
  if (bad.has(String(rawLast).trim().toUpperCase())) return null;
  return cleanLast(rawLast);
}

// 4) Count frequencies across all normalized collections
const counts = new Map();

for (const collName of SIMPLE_COLL_NAMES) {
  if (!db.getCollectionNames().includes(collName)) continue;

  const cur = db.getCollection(collName).find(
    {},
    { name: 1, full_name: 1, last: 1, last_name: 1, surname: 1, person: 1 }
  );

  while (cur.hasNext()) {
    const doc = cur.next();
    const ln = extractLastFromDoc(doc);
    if (!ln) continue;
    counts.set(ln, (counts.get(ln) || 0) + 1);
  }
}

// 5) Sort by frequency (desc), then alphabetically, and print ONE per line
[...counts.entries()]
  .sort((a, b) => {
    const d = b[1] - a[1];
    if (d !== 0) return d;
    if (a[0] === b[0]) return 0;
    return a[0] < b[0] ? -1 : 1;
  })
  .forEach(([ln]) => print(ln));