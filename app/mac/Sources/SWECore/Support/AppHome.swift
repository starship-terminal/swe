import Foundation

/// The home the app's own stores live under: the account's home folder,
/// or `SWE_APP_HOME` when a scratch launch sets it.
///
/// The app ignores `$HOME` (docs/app-architecture.md, "The app ignores
/// `$HOME`"): `homeDirectoryForCurrentUser` reads the account record. For
/// a manual check against a seeded scratch home, `SWE_APP_HOME` moves what
/// `AppModel` builds from the home (`~/.swe` with the ledger, the search
/// index, the transcript mirror, caches and accounts, and the harness
/// homes the ledger scans) while `HOME` moves the `swe` it spawns. Pages
/// that resolve the home themselves and the app's UserDefaults still use
/// the real ones. Production never sets it.
public enum AppHome {
    public static let environment = "SWE_APP_HOME"

    public static func url(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let path = environment[Self.environment], !path.isEmpty {
            return URL(fileURLWithPath: (path as NSString).expandingTildeInPath, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
    }
}
