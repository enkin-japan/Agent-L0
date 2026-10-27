"""Copy-only mutation proof for the build 78 iPhone unpair guards (29 mutants).

Canonical command, from the repo root (needs xcrun swiftc and backend/.venv):

    python3 apple-app/Tests/run_unpair_mutations.py            # all 29 mutants
    python3 apple-app/Tests/run_unpair_mutations.py --list
    python3 apple-app/Tests/run_unpair_mutations.py --only M1,M5

What it proves: every unpair guard in Sources/App/PhoneLink.swift,
Sources/Views/PhoneLinkViews.swift and Sources/App/PushManager.swift is held by either
the Swift harness apple-app/Tests/phone_link_test.swift (compiled with PhoneLink.swift
and LiveActivity/ApprovalRouting.swift, as backend/tests/test_phone_link.py does) or
the source wiring pins in test_phone_link.py::test_wiring. Each mutant names the check
expected to catch it; it counts as killed only when that check goes red (a mutant that
fails to compile reproduces no defect and is reported as invalid, not killed).

It never writes the real tree: per mutant it copies apple-app/Sources and project.yml to
a temp dir, mutates the copy, compiles/runs the Swift harness against the copy, runs
test_wiring with ROOT pointed at the copy (under backend/.venv/bin/python), and deletes
the copy. The three real source files' sha256 are checked before and after the run.
A baseline (unmutated copy) must be green on both checks first. Exit 0 only when every
selected mutant is killed and the real tree is unchanged.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PYTHON = REPO / "backend/.venv/bin/python"
HARNESS = REPO / "apple-app/Tests/phone_link_test.swift"
WIRING_TEST = REPO / "backend/tests/test_phone_link.py"

PL = "Sources/App/PhoneLink.swift"
VW = "Sources/Views/PhoneLinkViews.swift"
PM = "Sources/App/PushManager.swift"
SWIFT, WIRING = "swift", "wiring"

# (id, description, file under apple-app/, old_str, new_str, check expected to go red)
MUTANTS = [
    ("M1", "unpair hides the row", PL,
     "        for account in plan.clearedAccounts { Keychain.save(account, \"\") }\n",
     "        for account in plan.clearedAccounts { Keychain.save(account, \"\") }\n"
     "        hidden.insert(MacDirectory.hideKey(macId: nil, url: mac.url))\n", WIRING),
    ("M1b", "unpair drops the row", PL,
     "            out[i].credential = .none\n", "            out.remove(at: i)\n", SWIFT),
    ("M1c", "remove inserts into a dismissed set", PL,
     "        macs = plan.macs\n        if currentId == id {",
     "        macs = plan.macs\n        dismissed.insert(id)\n        if currentId == id {", WIRING),
    ("M2", "pairingState always paired", PL,
     "var pairingState: PairingState { credential == .none ? .unpaired : .paired }",
     "var pairingState: PairingState { .paired }", SWIFT),
    ("M2b", "badge switches on a constant, not mac.pairingState", VW,
     "        switch mac.pairingState {\n        case .paired:",
     "        switch SavedMac.PairingState.paired {\n        case .paired:", WIRING),
    ("M3", "unpair swipe offered on unpaired rows", VW,
     "                        if mac.pairingState == .paired {\n"
     "                            Button(L(\"取消配对\"), role: .destructive) { phoneLink.remove(mac.id) }\n"
     "                        }\n",
     "                        Button(L(\"取消配对\"), role: .destructive) { phoneLink.remove(mac.id) }\n", WIRING),
    ("M3b", "swipe label back to 删除", VW,
     'Button(L("取消配对"), role: .destructive)', 'Button(L("删除"), role: .destructive)', WIRING),
    ("M4", "merge grows a hidden filter", PL,
     "synced: [PhoneConnection], pinned: Set<String>,\n",
     "synced: [PhoneConnection], pinned: Set<String>, hidden: Set<String> = [],\n", WIRING),
    ("M4b", "merge skips the unpaired row's iCloud item", PL,
     "            if connection.macId == nil, tagged.contains(connection.url) { continue }\n",
     "            if connection.macId == nil, tagged.contains(connection.url) { continue }\n"
     "            if out.contains(where: { $0.macId == connection.macId && $0.credential == .none }) { continue }\n",
     SWIFT),
    ("Mx-ticket-kept", "unpair keeps the ticket account", PL,
     "clearedAccounts: [keyAccount(id), ticketAccount(id)]", "clearedAccounts: [keyAccount(id)]", SWIFT),
    ("Mx-no-forget", "unpair never asks the Mac to forget the device token", PL,
     "        let forget = out[i].credential == .deviceToken\n", "        let forget = false\n", SWIFT),
    ("M5", "new row adopts the shared key", PL,
     "                                   credential: .none, customName: false)",
     "                                   credential: legacyKey == nil ? .none : .sharedKey, customName: false)",
     SWIFT),
    ("M5b", "existing row adopts the shared key", PL,
     "            if let legacyKey { legacyKeys[out[i].id] = legacyKey }\n",
     "            if let legacyKey { legacyKeys[out[i].id] = legacyKey; "
     "if out[i].credential != .deviceToken { out[i].credential = .sharedKey } }\n", SWIFT),
    ("M5c", "readSynced stores the legacy key", PL,
     "        legacyKeys = merged.legacyKeys\n",
     "        legacyKeys = merged.legacyKeys\n"
     "        for (id, key) in merged.legacyKeys { Keychain.save(Self.keyAccount(id), key) }\n", WIRING),
    ("M5d", "tap skips the legacy upgrade", PL,
     "case .legacyUpgrade(let key): upgradeLegacy(id, key: key)", "case .legacyUpgrade: requestConnection(id)",
     WIRING),
    ("M6", "unpair ignores removedFromICloud", PL,
     "        if out[i].source == .icloud, !out[i].removedFromICloud {", "        if out[i].source == .icloud {",
     SWIFT),
    ("M7", "push routes to an unpaired source Mac", PL,
     "if let src, let mac = macs.first(where: { $0.macId == src }), mac.pairingState == .paired { return mac.id }",
     "if let src, let mac = macs.first(where: { $0.macId == src }) { return mac.id }", SWIFT),
    ("M7b", "push selects (pairs) instead of switching", PM,
     "            phoneLink.switchWithoutPairing(id)\n", "            phoneLink.select(id)\n", WIRING),
    ("M7c", "switchWithoutPairing starts a pairing", PL,
     "                                           freshCredential: false) else { return }",
     "                                           freshCredential: false) else { requestOrUpgrade(id); return }",
     WIRING),
    ("M8", "empty iCloud read drops rows", PL,
     "let dropped = synced.isEmpty ? [] :", "let dropped = false ? [] :", SWIFT),
    ("M8b", "drop ignores pinned", PL,
     "$0.credential == .none && !pinned.contains($0.id)", "$0.credential == .none", SWIFT),
    ("M8c", "drop removes credentialed rows", PL,
     "gone.filter { $0.credential == .none && !pinned.contains($0.id) }", "gone.filter { !pinned.contains($0.id) }",
     SWIFT),
    ("M8d", "current Mac not pinned", PL,
     "[currentId, handshake?.macId, legacyUpgradeId, requesting?.id]",
     "[handshake?.macId, legacyUpgradeId, requesting?.id]", WIRING),
    ("M9", "dropped row keeps its key", PL,
     "            Keychain.save(Self.ticketAccount(id), \"\")\n            Keychain.save(Self.keyAccount(id), \"\")\n",
     "            Keychain.save(Self.ticketAccount(id), \"\")\n", WIRING),
    ("M10", "shared-key row with a missing key does not upgrade", PL,
     "credential == .none || (credential == .sharedKey && !hasKey)", "credential == .none", SWIFT),
    ("M10b", "device token falls back to the shared key", PL,
     "credential == .none || (credential == .sharedKey && !hasKey)", "credential == .none || !hasKey", SWIFT),
    ("M11", "legacy upgrade re-entrant", PL,
     "        guard legacyUpgradeId == nil, let mac = macs.first(where: { $0.id == id }),\n              let base",
     "        guard let mac = macs.first(where: { $0.id == id }),\n              let base", WIRING),
    ("M11b", "legacy upgrade task cancellable as a handshake", PL,
     "        Task { [weak self] in\n            do {\n                let issued = try await PairingAPI.upgrade(",
     "        handshakeTask = Task { [weak self] in\n            do {\n                let issued = try await PairingAPI.upgrade(",
     WIRING),
    ("M11c", "legacy upgrade shows no waiting state", PL,
     "        handshake = Handshake(macId: id, macName: mac.name, code: \"\", state: .waiting)\n", "", WIRING),
]

WIRING_SNIPPET = (
    "import importlib.util, sys\n"
    "from pathlib import Path\n"
    "spec = importlib.util.spec_from_file_location('tpl', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
    "mod.ROOT = Path(sys.argv[2])\n"
    "try:\n"
    "    mod.test_wiring()\n"
    "except AssertionError as e:\n"
    "    print(str(e)[:200]); sys.exit(1)\n"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def swift_check(root: Path, work: Path):
    """('green'|'red'|'compile-error', detail)."""
    binary = work / "phone_link_test"
    c = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(work / "modules"),
                        str(root / "apple-app" / PL),
                        str(root / "apple-app/Sources/LiveActivity/ApprovalRouting.swift"),
                        str(HARNESS), "-o", str(binary)], capture_output=True, text=True, timeout=300)
    if c.returncode:
        return "compile-error", c.stderr.strip().splitlines()[:1]
    r = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
    return ("red" if r.returncode else "green"), fails


def wiring_check(root: Path):
    r = subprocess.run([str(PYTHON), "-c", WIRING_SNIPPET, str(WIRING_TEST), str(root)],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if r.returncode == 0:
        return "green", []
    if r.returncode == 1 and "Traceback" not in r.stderr:
        return "red", [r.stdout.strip() or "(assertion without message)"]
    return "error", (r.stderr.strip().splitlines() or ["?"])[-1:]


def make_copy(tmp: Path) -> Path:
    shutil.copytree(REPO / "apple-app/Sources", tmp / "apple-app/Sources")
    shutil.copy2(REPO / "apple-app/project.yml", tmp / "apple-app/project.yml")
    return tmp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma-separated mutant ids (see --list)")
    ap.add_argument("--list", action="store_true", help="list mutants and exit")
    args = ap.parse_args()

    by_id = {m[0]: m for m in MUTANTS}
    if args.list:
        for m in MUTANTS:
            print(f"{m[0]:15} {m[2]:36} expect red: {m[5]:7} {m[1]}")
        return 0
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            print(f"unknown mutant id(s): {unknown}; see --list")
            return 2
        selected = [by_id[w] for w in wanted]
    else:
        selected = list(MUTANTS)
    if not PYTHON.exists():
        print(f"venv python not found: {PYTHON}")
        return 2

    real = [REPO / "apple-app" / f for f in (PL, VW, PM)]
    before = {p: sha256(p) for p in real}
    for m in selected:
        count = (REPO / "apple-app" / m[2]).read_text(encoding="utf-8").count(m[3])
        if count != 1:
            print(f"{m[0]}: original snippet occurs {count} times in {m[2]}; refusing to run")
            return 2

    with tempfile.TemporaryDirectory(prefix="unpair-baseline-") as d:
        root = make_copy(Path(d))
        sw, sw_detail = swift_check(root, Path(d))
        wi, wi_detail = wiring_check(root)
    if sw != "green" or wi != "green":
        print(f"baseline not green (swift={sw} {sw_detail}, wiring={wi} {wi_detail}); mutants would prove nothing")
        return 2
    print("baseline green: swift harness + wiring pins\n")

    rows, ok = [], True
    for mid, desc, rel, old, new, expect in selected:
        with tempfile.TemporaryDirectory(prefix=f"unpair-{mid}-") as d:
            root = make_copy(Path(d))
            target = root / "apple-app" / rel
            target.write_text(target.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
            sw, sw_detail = swift_check(root, Path(d)) if rel == PL else ("n/a", [])
            wi, wi_detail = wiring_check(root)
        if sw == "compile-error" or wi == "error":
            result = "INVALID (" + ("compile-error" if sw == "compile-error" else "wiring error") + ")"
            killed = False
        else:
            killed = (sw if expect == SWIFT else wi) == "red"
            result = "killed" if killed else "SURVIVED"
        ok = ok and killed
        detail = sw_detail if expect == SWIFT else wi_detail
        shown = "; ".join(detail[:3]) + (f"; ... (+{len(detail) - 3})" if len(detail) > 3 else "")
        rows.append((mid, desc, expect, result, f"swift={sw} wiring={wi}", shown or "-"))
        print(f"{mid}: {result} (swift={sw}, wiring={wi})", flush=True)

    untouched = all(sha256(p) == h for p, h in before.items())
    ok = ok and untouched
    print("\n| mutant | defect | expected | result | checks | red evidence |")
    print("|---|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    print(f"\nreal tree untouched (sha256 of {PL}, {VW}, {PM}): {'yes' if untouched else 'NO'}")
    print("ALL MUTANTS KILLED, REAL TREE UNTOUCHED" if ok else "FAILURE: survivor, invalid mutant or tree changed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
