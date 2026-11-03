// Checks for iOS/OfflinePlan.swift (download bookkeeping, no device needed). CI:
//   swiftc -o /tmp/offline_plan_check iOS/OfflinePlan.swift tools/offline_plan_check/main.swift && /tmp/offline_plan_check
import Foundation

var failures = 0

func check(_ ok: Bool, _ label: String) {
    print((ok ? "PASS " : "FAIL ") + label)
    if !ok { failures += 1 }
}

func entry(_ id: Int, _ source: Int, _ rev: String = "r1") -> OfflineEntry {
    OfflineEntry(id: id, source: source, rev: rev)
}

// Reference counting: a file counts once per list, also when a list holds it twice.
let refs = OfflinePlan.references([[1, 2, 2, 3], [3, 4]])
check(refs == [1: 1, 2: 1, 3: 2, 4: 1], "references: per list, duplicates once")

// Removing a list deletes only what no other list needs.
check(OfflinePlan.orphans(removing: [1, 2, 3], keeping: [[3, 4]]) == [1, 2], "remove list: shared file kept")
check(OfflinePlan.orphans(removing: [1, 2], keeping: []) == [1, 2], "remove the only list: all its files go")
check(OfflinePlan.orphans(removing: [5], keeping: [[5], [6]]).isEmpty, "remove list: file in another list stays")

// List change: new entries fetched, removed ones deleted unless another list has them, re-scanned files fetched again.
let old = [entry(10, 1), entry(11, 2), entry(12, 3)]
let new = [entry(10, 1), entry(12, 3, "r2"), entry(13, 4), entry(14, 4)]
let have: [Int: String] = [1: "r1", 2: "r1", 3: "r1"]
var d = OfflinePlan.diff(old: old, new: new, others: [], have: have)
check(d.fetch == [3, 4], "diff: changed rev and new file fetched, each once (got \(d.fetch))")
check(d.remove == [2], "diff: dropped entry's file removed (got \(d.remove))")
d = OfflinePlan.diff(old: old, new: new, others: [[2]], have: have)
check(d.remove.isEmpty, "diff: dropped file kept while another list needs it")
d = OfflinePlan.diff(old: old, new: old, others: [], have: have)
check(d.fetch.isEmpty && d.remove.isEmpty, "diff: unchanged list -> nothing to do")
// A song entry whose download switched to its better video: the old file goes, the new one comes.
d = OfflinePlan.diff(old: [entry(10, 1)], new: [entry(10, 7)], others: [], have: [1: "r1"])
check(d.fetch == [7] && d.remove == [1], "diff: entry now served by another version")

// Size before a download: only what is missing, each file once; the 1 GB reserve.
let sizes: [Int: Int64] = [1: 100, 3: 300, 4: 400]
check(OfflinePlan.bytesNeeded(new, sizes: sizes, have: have) == 700, "bytes needed: missing / changed files once")
check(OfflinePlan.fits(500_000_000, free: 1_600_000_000), "space: fits with 1 GB to spare")
check(!OfflinePlan.fits(700_000_000, free: 1_600_000_000), "space: refused without 1 GB to spare")

check(OfflinePlan.formatLabel("flac 24/48000") == "FLAC 24/48", "format label FLAC 24/48")
check(OfflinePlan.formatLabel("flac 16/44100") == "FLAC 16/44.1", "format label FLAC 16/44.1")
check(OfflinePlan.formatLabel("opus 48000") == "OPUS 48", "format label OPUS 48")
check(OfflinePlan.formatLabel("mp3") == "MP3" && OfflinePlan.formatLabel(nil) == nil, "format label codec only / none")

print(failures == 0 ? "ALL PASS" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
