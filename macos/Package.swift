// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "LedgePill",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "LedgePill")
    ]
)
