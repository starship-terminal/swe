// swift-tools-version: 6.2
import PackageDescription

// The session view's native shell, shared by the Mac and iPhone apps
// (plan milestones G2, G17, G18): a `swe-hub://` scheme handler that
// forwards the page's requests to one pinned hub and adds the device's
// credential, the web view settings the session view needs, an offline
// cache hook and the decode side of the `sweNative` bridge. No app wires
// it in yet; `Proof/` is the throwaway iPhone harness that tested it.
let package = Package(
    name: "SessionWebShell",
    // The deployment targets of the two apps that will link it:
    // app/ios/project.yml (iOS 17) and app/mac/Package.swift (macOS 26).
    platforms: [.iOS(.v17), .macOS(.v26)],
    products: [
        .library(name: "SessionWebShell", targets: ["SessionWebShell"]),
    ],
    targets: [
        .target(name: "SessionWebShell"),
        .testTarget(name: "SessionWebShellTests", dependencies: ["SessionWebShell"]),
    ]
)
