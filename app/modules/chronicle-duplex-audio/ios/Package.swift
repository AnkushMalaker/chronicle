// swift-tools-version: 5.9

import PackageDescription

let package = Package(
  name: "ChronicleDuplexAudio",
  platforms: [
    .macOS(.v13),
    .iOS(.v15),
  ],
  products: [
    .library(name: "ChronicleDuplexAudio", targets: ["ChronicleDuplexAudio"]),
  ],
  targets: [
    .target(
      name: "ChronicleDuplexAudio",
      path: ".",
      exclude: [
        "ChronicleDuplexAudio.podspec",
        "ChronicleDuplexAudioModule.swift",
        "Package.swift",
        "Tests",
      ],
      sources: ["DuplexAudioState.swift"]
    ),
    .testTarget(
      name: "ChronicleDuplexAudioTests",
      dependencies: ["ChronicleDuplexAudio"],
      path: "Tests"
    ),
  ]
)
