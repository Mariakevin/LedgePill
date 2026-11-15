// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "LedgePill",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "LedgePill")
    ],
    swiftLanguageModes: [.v5]
)
