// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "SWE",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "SWE", targets: ["SWEApp"]),
        .executable(name: "SWEWidgets", targets: ["SWEWidgets"]),
        .executable(name: "SWEFanHelper", targets: ["SWEFanHelper"]),
    ],
    dependencies: [
        // In-app updates. Pinned exactly: this framework ships signed inside
        // the bundle, so upgrades are deliberate. The feed is appcast.xml on
        // the latest GitHub release (set in build-app.sh's prod Info.plist).
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.0"),
    ],

    targets: [
        // Everything testable: models, the swe client, scanners, view models.
        // Meta's zstd v1.5.7 single-file decoder. Its own target because
        // SwiftPM refuses C and Swift in one. The .inc is the amalgam and
        // is included by zstd_read.c, so it is not a compile unit.
        .target(
            name: "CZStd",
            path: "Sources/CZStd",
            exclude: ["zstddeclib.inc"],
            publicHeadersPath: "include",
            cSettings: [.unsafeFlags(["-I", "Sources/CZStd"])]
        ),
        .target(
            name: "SWECore",
            dependencies: ["CZStd"],
            resources: [.process("Resources")]
        ),
        // The widget views, shared by the extension that draws them and the
        // app's Desktop widgets previews.
        .target(name: "SWEWidgetsUI", dependencies: ["SWECore"]),
        // The SwiftUI shell. Kept thin so behaviour lives in SWECore.
        .executableTarget(
            name: "SWEApp",
            dependencies: [
                "SWECore", "SWEWidgetsUI",
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            // Sparkle.framework is copied into Contents/Frameworks by
            // build-app.sh; the executable finds it through this rpath.
            linkerSettings: [.unsafeFlags([
                "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks",
            ])]
        ),
        // The WidgetKit extension. SwiftPM builds it as a plain executable;
        // script/build-app.sh wraps it in an .appex inside SWE.app. The
        // entry point must be _NSExtensionMain (which Xcode sets on extension
        // targets): without it the Swift main returns at launch and the
        // extension exits before chronod can read its widgets.
        .executableTarget(
            name: "SWEWidgets",
            dependencies: ["SWECore", "SWEWidgetsUI"],
            linkerSettings: [.unsafeFlags(["-Xlinker", "-e", "-Xlinker", "_NSExtensionMain"])]
        ),
        // The root fan helper the fan popover installs. Its own executable
        // with SWECore only: it runs from /Library/PrivilegedHelperTools,
        // where the app's Sparkle rpath finds nothing.
        .executableTarget(name: "SWEFanHelper", dependencies: ["SWECore"]),
        .testTarget(
            name: "SWECoreTests",
            dependencies: ["SWECore"],
            resources: [.copy("Fixtures")]
        ),
        .testTarget(
            name: "SWEWidgetsUITests",
            dependencies: ["SWEWidgetsUI"]
        ),
        .testTarget(name: "SWEAppUITests", dependencies: ["SWEApp", "SWECore"]),
    ]
)
